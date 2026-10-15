"""What the web page and the bot share: request checks, the watchlist, the daily run."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from vnbot.jobs import JobQueue, prune_cache
from vnbot.settings import Settings

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
        self._path.write_text(json.dumps(codes), encoding="utf-8")

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


class DailyScheduler:
    """Each trading day at ``daily_report_time`` (Vietnam time): the trading bot's
    session, the watchlist's full analyses, then the nightly calibration."""

    RETRY_SECONDS = 600

    def __init__(self, settings: Settings, jobs: JobQueue, watchlist: Watchlist, bot=None, notify=None):
        self.settings, self.jobs, self.watchlist = settings, jobs, watchlist
        self.bot = bot
        self.notify = notify or (lambda text: None)
        self._stamp = settings.data_dir / "last_daily_run.txt"
        self._next_try = 0.0
        self._thread = threading.Thread(target=self._loop, name="daily-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _due(self) -> str | None:
        from tradingagents.market import vn_rules

        now = vn_rules.now_vn()
        if now.weekday() >= 5 or now.strftime("%H:%M") < self.settings.daily_report_time:
            return None
        day = now.strftime("%Y-%m-%d")
        last = self._stamp.read_text(encoding="utf-8").strip() if self._stamp.exists() else ""
        return None if last == day else day

    @staticmethod
    def _is_trading_day(day: str) -> bool:
        """VNINDEX has a bar for ``day``: weekday holidays (Tết, 30/4, 2/9...) have none.

        Raises the vendor's error when the data cannot be read, so the run is
        retried later rather than skipped.
        """
        from tradingagents.dataflows.ohlcv import load_ohlcv
        from tradingagents.market import vn_rules

        if vn_rules.is_market_hours():
            return True   # today's bar is held back until 15:00; a weekday is assumed open
        frame = load_ohlcv(vn_rules.BENCHMARK, day)
        return bool(len(frame)) and frame["Date"].iloc[-1].strftime("%Y-%m-%d") == day

    def _trade(self, day: str) -> None:
        if self.bot is None or self.settings.trade_mode == "off":
            return
        try:
            self.notify(self.bot.run_day(day))
        except Exception as exc:  # noqa: BLE001 - the watchlist run still goes ahead
            logger.exception("Trading bot session %s failed", day)
            self.notify(f"❌ Bot giao dịch lỗi phiên {day}: {exc}")

    def _calibrate(self, day: str) -> None:
        if self.bot is None or not self.settings.calibrate or self.settings.trade_mode == "off":
            return
        try:
            self.notify(self.bot.calibrate(day))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Calibration %s failed", day)
            self.notify(f"⚠️ Hiệu chỉnh {day} không chạy được: {exc}")

    def _loop(self) -> None:
        while True:
            try:
                day = self._due() if time.time() >= self._next_try else None
                if day:
                    try:
                        trading = self._is_trading_day(day)
                    except Exception as exc:  # noqa: BLE001 - data source down: try again later
                        logger.warning("Daily run %s: cannot read VNINDEX (%s); retrying in %ds",
                                       day, exc, self.RETRY_SECONDS)
                        self._next_try = time.time() + self.RETRY_SECONDS
                        continue
                    self._stamp.write_text(day, encoding="utf-8")
                    if not trading:
                        logger.info("Daily run %s skipped: no VNINDEX session that day (holiday)", day)
                        continue
                    removed = prune_cache(self.settings)
                    self._trade(day)
                    codes = self.watchlist.get()
                    logger.info("Daily run %s: %d codes, %d stale cache files removed", day, len(codes), removed)
                    for code in codes:
                        self.jobs.submit(code, day, "daily")
                    self._calibrate(day)
            except Exception:  # noqa: BLE001 - the scheduler must keep ticking
                logger.exception("Daily scheduler tick failed")
            time.sleep(30)
