"""What the web page and the bot share: request checks, the watchlist, the daily run."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from vnbot.jobs import JobQueue, prune_cache, write_text_atomic
from vnbot.settings import MIN_TRADE_TIME, Settings

logger = logging.getLogger(__name__)


def today_vn() -> str:
    from tradingagents.market import vn_rules

    return vn_rules.now_vn().strftime("%Y-%m-%d")


def check_request(ticker: str, date: str | None = None) -> tuple[str, str]:
    """A canonical stock code and a YYYY-MM-DD date no later than today; ValueError otherwise."""
    from tradingagents.dataflows.symbols import is_valid_code, normalize_symbol
    from tradingagents.market import vn_rules

    code = normalize_symbol((ticker or "").strip())
    if not code or not is_valid_code(code):
        raise ValueError(f"Mã không hợp lệ: {ticker!r}. Ví dụ: FPT, HPG, VCB.")
    if vn_rules.is_index(code):
        raise ValueError(f"{code} là chỉ số, không có quyết định giao dịch. Hãy nhập mã cổ phiếu.")
    day = (date or "").strip() or today_vn()
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Ngày không hợp lệ: {day!r}. Dùng dạng YYYY-MM-DD.") from None
    if parsed > today_vn():
        raise ValueError("Không phân tích được ngày trong tương lai.")
    return code, parsed


def quick_text(ticker: str, date: str | None = None) -> str:
    """The quantitative engine's decision alone (no LLM), as text."""
    from tradingagents.quant import engine

    code, day = check_request(ticker, date)
    return engine.analyze(code, day).deterministic_summary


class Watchlist:
    def __init__(self, settings: Settings):
        self._path: Path = settings.data_dir / "watchlist.json"
        self._lock = threading.Lock()
        if not self._path.exists():
            self._write(list(settings.watchlist_default))

    def _write(self, codes: list[str]) -> None:
        write_text_atomic(self._path, json.dumps(codes))

    def get(self) -> list[str]:
        with self._lock:
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []

    def set(self, codes: list[str]) -> list[str]:
        clean = []
        for raw in codes:
            code, _ = check_request(raw)
            if code not in clean:
                clean.append(code)
        with self._lock:
            self._write(clean[:30])
        return clean[:30]


# A weekday whose VNINDEX bar is still missing at this time (Vietnam) is a holiday;
# before it, a missing bar is taken as late (the provider has not published it yet).
LATE_BAR_CUTOFF = "18:00"


def _bar_may_be_late(day: str) -> bool:
    """``day`` is today, a weekday, and its bar could still be published."""
    from tradingagents.market import vn_rules

    now = vn_rules.now_vn()
    return (day == now.strftime("%Y-%m-%d") and now.weekday() < 5
            and now.strftime("%H:%M") < LATE_BAR_CUTOFF)


class DailyScheduler:
    """Two daily runs (Vietnam time):

    - from ``trade_time`` (never before 15:05, when the day's bar is final): the
      trading bot catches up every session it has not processed yet, then the
      nightly calibration runs on the last processed session. A failed run is
      retried with a growing delay (``MAX_TRADE_ATTEMPTS`` per day, the owner is
      told once); what is still missing is caught up by the next run. The bot
      also catches up once at startup, so a redeploy loses no session. On a
      weekday whose bar is not published yet (the run reached only the day
      before), the day is not stamped: it is tried again every
      ``LATE_BAR_RETRY_SECONDS`` until ``LATE_BAR_CUTOFF``, then taken as a holiday.
    - weekdays at ``daily_report_time``: the watchlist's full analyses (a missing
      bar after the close is likewise waited for until ``LATE_BAR_CUTOFF``).
    """

    RETRY_SECONDS = 600            # first retry; doubles per failure up to MAX_RETRY_SECONDS
    MAX_RETRY_SECONDS = 3600
    MAX_TRADE_ATTEMPTS = 6
    LATE_BAR_RETRY_SECONDS = 600

    def __init__(self, settings: Settings, jobs: JobQueue, watchlist: Watchlist, bot=None, notify=None):
        self.settings, self.jobs, self.watchlist = settings, jobs, watchlist
        self.bot = bot
        self.notify = notify or (lambda text: None)
        self._stamp = settings.data_dir / "last_daily_run.txt"          # the watchlist run
        self._trade_stamp = settings.data_dir / "last_trade_run.txt"    # the trading run
        self._cal_stamp = settings.data_dir / "last_calibration.txt"    # the session last calibrated
        self._next_try = 0.0
        self._trade_next_try = 0.0
        self._trade_failures = 0
        self._alerted = ""          # the day whose failure the owner was already told about
        self._session: str | None = None
        self._started = False
        self._thread = threading.Thread(target=self._loop, name="daily-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _due(self) -> str | None:
        """The day of the watchlist run, when it is due."""
        from tradingagents.market import vn_rules

        now = vn_rules.now_vn()
        if now.weekday() >= 5 or now.strftime("%H:%M") < self.settings.daily_report_time:
            return None
        day = now.strftime("%Y-%m-%d")
        return None if self._read(self._stamp) == day else day

    def _trade_due(self) -> str | None:
        """Today, once per day from ``trade_time`` (weekends too: a catch-up of a missed Friday)."""
        from tradingagents.market import vn_rules

        now = vn_rules.now_vn()
        trade_time = max(self.settings.trade_time, MIN_TRADE_TIME)
        if now.strftime("%H:%M") < trade_time:
            return None
        day = now.strftime("%Y-%m-%d")
        return None if self._read(self._trade_stamp) == day else day

    @staticmethod
    def _is_trading_day(day: str) -> bool:
        """VNINDEX has a bar for ``day``: weekday holidays (Tết, 30/4, 2/9...) have none.

        Before ``day``'s bar is final (15:05) there is nothing to look at yet: a
        weekday is assumed open. Raises the vendor's error when the data cannot be
        read, so the run is retried later rather than skipped.
        """
        from tradingagents.dataflows.ohlcv import load_ohlcv
        from tradingagents.market import vn_rules

        now = vn_rules.now_vn()
        if day >= now.strftime("%Y-%m-%d") and now.strftime("%H:%M") < MIN_TRADE_TIME:
            return True
        frame = load_ohlcv(vn_rules.BENCHMARK, day)
        return bool(len(frame)) and frame["Date"].iloc[-1].strftime("%Y-%m-%d") == day

    def _trade(self, day: str) -> bool:
        """Bring the books up to ``day``; True when done (or nothing to do)."""
        self._session = None
        if self.bot is None or self.settings.trade_mode == "off":
            return True
        try:
            if hasattr(self.bot, "catch_up"):
                result = self.bot.catch_up(day)
                self._session = result.day
                if result.new:
                    self.notify(result.text)
            else:
                self.notify(self.bot.run_day(day))
                self._session = day
        except Exception as exc:  # noqa: BLE001 - retried; the watchlist run still goes ahead
            logger.exception("Trading bot run %s failed", day)
            if self._alerted != day:
                self._alerted = day
                self.notify(f"❌ Bot giao dịch lỗi phiên {day}: {exc}. "
                            f"Bot sẽ tự thử lại (tối đa {self.MAX_TRADE_ATTEMPTS} lần hôm nay) và chạy bù phiên bị lỡ.")
            return False
        return True

    def _calibrate(self, day: str) -> None:
        if self.bot is None or not self.settings.calibrate or self.settings.trade_mode == "off":
            return
        try:
            self.notify(self.bot.calibrate(day))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Calibration %s failed", day)
            self.notify(f"⚠️ Hiệu chỉnh {day} không chạy được: {exc}")

    def _run_trading(self, day: str, *, scheduled: bool) -> None:
        """One trading attempt: on success the stamp (scheduled runs) and the calibration of
        the processed session (once per session); on failure a later retry."""
        if self._trade(day):
            self._trade_failures, self._trade_next_try = 0, 0.0
            session = self._session
            if scheduled and session and session < day and _bar_may_be_late(day):
                # Today's bar is not out yet (a late provider): not done, try again soon.
                logger.info("Trading run %s reached only %s; today's bar not published yet, retrying in %ds",
                            day, session, self.LATE_BAR_RETRY_SECONDS)
                self._trade_next_try = time.time() + self.LATE_BAR_RETRY_SECONDS
                return
            if scheduled:
                write_text_atomic(self._trade_stamp, day)
            if scheduled and session and self._read(self._cal_stamp) != session:
                write_text_atomic(self._cal_stamp, session)     # one attempt per session
                self._calibrate(session)
            return
        self._trade_failures += 1
        if self._trade_failures >= self.MAX_TRADE_ATTEMPTS:
            logger.error("Trading run %s failed %d times; next attempt at the next run", day, self._trade_failures)
            if scheduled:
                write_text_atomic(self._trade_stamp, day)
            self.notify(f"⚠️ Bot giao dịch lỗi {self._trade_failures} lần ngày {day}; tạm dừng tới lần chạy sau "
                        "(các phiên bị lỡ sẽ được chạy bù), hoặc gửi /chay khi dữ liệu ổn định.")
            self._trade_failures, self._trade_next_try = 0, 0.0
            return
        delay = min(self.RETRY_SECONDS * 2 ** (self._trade_failures - 1), self.MAX_RETRY_SECONDS)
        logger.warning("Trading run %s failed (attempt %d); retrying in %ds", day, self._trade_failures, delay)
        self._trade_next_try = time.time() + delay

    def _report(self, day: str) -> None:
        """The watchlist's analyses of ``day``; the stamp only once they are queued."""
        try:
            trading = self._is_trading_day(day)
        except Exception as exc:  # noqa: BLE001 - data source down: try again later
            logger.warning("Daily run %s: cannot read VNINDEX (%s); retrying in %ds", day, exc, self.RETRY_SECONDS)
            self._next_try = time.time() + self.RETRY_SECONDS
            return
        if not trading and _bar_may_be_late(day):
            logger.info("Daily run %s: today's VNINDEX bar not published yet; retrying in %ds",
                        day, self.RETRY_SECONDS)
            self._next_try = time.time() + self.RETRY_SECONDS
            return
        if not trading:
            logger.info("Daily run %s skipped: no VNINDEX session that day (holiday)", day)
            write_text_atomic(self._stamp, day)
            return
        removed = prune_cache(self.settings)
        codes = self.watchlist.get()
        logger.info("Daily run %s: %d codes, %d stale cache files removed", day, len(codes), removed)
        for code in codes:
            self.jobs.submit(code, day, "daily")
        write_text_atomic(self._stamp, day)

    def tick(self) -> None:
        from tradingagents.market import vn_rules

        if not self._started:
            self._started = True
            if self.bot is not None and self.settings.trade_mode != "off":
                # Catch up the sessions missed while the server was down (final ones only).
                self._run_trading(vn_rules.now_vn().strftime("%Y-%m-%d"), scheduled=False)
        day = self._trade_due()
        if day and time.time() >= self._trade_next_try:
            self._run_trading(day, scheduled=True)
        day = self._due() if time.time() >= self._next_try else None
        if day:
            self._report(day)

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the scheduler must keep ticking
                logger.exception("Daily scheduler tick failed")
            time.sleep(30)
