"""The trading bot: three paper accounts traded every session, signals for a real one.

Three strategies run side by side, each on its own paper book:

- ``factor`` (the default for signals): the defensive trend book - low
  volatility and nearness to the 52-week high among the 50 largest liquid HOSE
  stocks, held only while VNINDEX is above its 200-session average
  (trading/factor.py; chosen by the independent evaluation of 2026-10-15).
- ``engine``: the Quant Engine's BUY plans (entry zone, stop, T1/T2).
- ``momentum``: the older momentum rotation over today's VN30
  (trading/momentum.py), kept as a comparison book.

The report compares them every day; ``SIGNAL_STRATEGY`` picks which one the
real-account signals follow. After the close (``run_day`` / ``catch_up``):

1. Every VNINDEX session since a book's last processed day is replayed in
   order, each with the data up to that session only (no look-ahead): the
   orders of the session before fill or lapse, stops and targets are checked
   on the session's bar (ledger rules, T+2, fees, slippage). A session whose
   bar is missing or not final is never processed: it waits for the next run.
2. The engine decides for the basket (VN30 + watchlist + anything held), with
   the paper book as its portfolio so held stocks are judged as holdings.
3. The paper account places the next session's orders; the NAV is recorded
   beside VNINDEX. Each book saves after each session with its own
   ``meta["last_day"]``, so a run that fails half-way is resumed, not replayed.
4. In ``signal`` mode, the decisions of the last session are turned into
   suggested orders sized on the real book, and that session's stop/target
   hits on real holdings are flagged. The real book changes only when its
   owner reports a fill (/khop) or the cash (/von); the bot never writes it.

Then, if enabled, the nightly calibration may move one threshold of the
engine and one of the momentum rotation (see ``tradingagents.trading.calibrate``);
the defensive book keeps the parameters that were evaluated. No order is ever
sent to a broker.
"""

from __future__ import annotations

import bisect
import copy
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from pathlib import Path

from vnbot.jobs import write_text_atomic
from vnbot.settings import MIN_TRADE_TIME, Settings

logger = logging.getLogger(__name__)

# Sessions replayed after downtime. Older missed sessions are skipped (their
# orders lapse) and the report says so: weeks of replay would take long and
# the decisions would be stale anyway.
MAX_CATCH_UP = 20

# After the listing (exchange of each stock) failed to load, runs use the HOSE
# tick grid for this long instead of waiting on the listing again.
LISTING_RETRY_SECONDS = 1800
_listing_down_until = 0.0

# A session is not saved when the price sources did not answer for a held stock or
# for more than this share of the basket: it is retried later (the source breaker
# turns a short outage into minutes of failures for every symbol).
MAX_UNAVAILABLE_SHARE = 0.2

# The defensive book's universe: one build per session it is needed on (month ends,
# the market filter turning on), cached; after a failed build the last one this many
# days old at most is used, else the VN30 members.
UNIVERSE_FILE = "factor_universe.json"
UNIVERSE_KEEP = 12


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}".replace(",", ".")


def _vnd(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".") + " đồng"


def _bn(value: float) -> str:
    return f"{value / 1e6:,.1f} tr".replace(",", "X").replace(".", ",").replace("X", ".")


def _now_vn():
    from tradingagents.market import vn_rules

    return vn_rules.now_vn()


def today_vn() -> str:
    return _now_vn().strftime("%Y-%m-%d")


def final_day(now=None) -> str:
    """The latest calendar day whose session (if there was one) is final: today from
    MIN_TRADE_TIME (Vietnam time), else yesterday."""
    now = now or _now_vn()
    day = now.date()
    if now.strftime("%H:%M") < MIN_TRADE_TIME:
        day -= timedelta(days=1)
    return day.isoformat()


def _book_day(book) -> str:
    """The last session a book has processed ("" for a new book). Books saved before
    ``meta["last_day"]`` existed (the momentum book) fall back to their NAV history."""
    day = book.meta.get("last_day")
    if day:
        return str(day)
    return str(book.history[-1]["date"]) if book.history else ""


def _pending(sessions: list[str], last: str) -> tuple[list[str], int]:
    """The sessions a book at ``last`` still has to process, and how many are skipped (cap)."""
    if not last:
        return sessions[-1:], 0          # a new book starts at the latest session, no backfill
    after = [s for s in sessions if s > last]
    skipped = max(0, len(after) - MAX_CATCH_UP)
    return after[skipped:], skipped


def _expire(book, prev: str | None) -> list:
    """Drop the orders whose session has passed without being processed.

    An order created on D is for the session after D. Before processing session S
    (previous session ``prev``), an order created before ``prev`` was meant for a
    session up to ``prev``: it must lapse rather than fill at S's prices.
    """
    if not prev:
        return []
    stale = [o for o in book.orders if o.created < prev]
    if stale:
        book.orders = [o for o in book.orders if o.created >= prev]
    return stale


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _unavailable(exc: BaseException) -> bool:
    """Whether ``exc`` (or what caused it) is a price source that did not answer."""
    from tradingagents.dataflows.errors import VendorUnavailableError

    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, VendorUnavailableError):
            return True
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


def _add_weekdays(day: str, n: int) -> str:
    """``n`` weekdays after ``day`` (holidays are not known)."""
    d = _date.fromisoformat(day[:10])
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.isoformat()


def _pending_lots(pos, day: str) -> list[tuple[str, int]]:
    """(T+2 day, shares) of a real holding's lots that cannot be sold all through the
    session after ``day``: shares bought on T arrive in the afternoon of T+2."""
    from tradingagents.trading.ledger import SETTLEMENT_SESSIONS, _sessions_between

    due: dict[str, int] = {}
    for lot in pos.unsettled:
        if max(lot["sessions"], _sessions_between(lot["date"], day)) < SETTLEMENT_SESSIONS:
            t2 = _add_weekdays(lot["date"], SETTLEMENT_SESSIONS)
            due[t2] = due.get(t2, 0) + lot["qty"]
    return sorted(due.items())


def _t2_note(pending: list[tuple[str, int]], day: str) -> str:
    nxt = _add_weekdays(day, 1)
    return "; ".join(f"{_fmt(qty)} cp chưa về đủ T+2: chỉ bán được từ chiều "
                     + (f"phiên tới ({t2})" if t2 <= nxt else t2) + " - đặt lệnh phiên chiều/ATC"
                     for t2, qty in pending)


def _sell_advice(pos, day: str, verb: str = "BÁN", when: str = "") -> str:
    """What of a real holding can be sold next session, and when the rest can (T+2)."""
    pending = _pending_lots(pos, day)
    if not pending:
        return f"đề xuất {verb} {_fmt(pos.qty)} cp{when}"
    free = max(0, pos.qty - sum(qty for _, qty in pending))
    head = f"đề xuất {verb} {_fmt(free)} cp{when}; " if free else ""
    return head + _t2_note(pending, day)


def _adjustment_lines(book, day: str) -> list[str]:
    """The day's corporate-action adjustments of a paper book (ledger ``_rebase``)."""
    return [f"- ⚖️ {a['symbol']}: giá điều chỉnh quyền (cổ tức/thưởng) x{a['ratio']:.3f} - "
            f"{_fmt(a['before'])} → {_fmt(a['after'])} cp, stop/T1/T2 điều chỉnh theo"
            for a in book.meta.get("adjustments") or [] if isinstance(a, dict) and a.get("date") == day]


@dataclass
class RunResult:
    day: str | None              # the last session the books are at
    text: str                    # the report (the previous one when nothing was new)
    new: bool = False            # whether this run processed at least one session
    sessions: list[str] = field(default_factory=list)   # processed by this run, in order


class _Market:
    """Daily bars for one run, read once per symbol up to ``as_of`` and sliced per session.

    Bars come unfilled (``fill_gaps=False``): a session with missing prices is a
    session without trading, never a forward-filled copy of the day before.
    """

    def __init__(self, as_of: str):
        self.as_of = as_of
        self._frames: dict[str, tuple | Exception | None] = {}
        self._boards: dict[str, str | None] | None = None

    def _entry(self, symbol: str):
        if symbol not in self._frames:
            from tradingagents.dataflows import ohlcv
            from tradingagents.dataflows.errors import NoMarketDataError, VendorError

            try:
                frame = ohlcv.load_ohlcv(symbol, self.as_of, fill_gaps=False)
                dates = [d.strftime("%Y-%m-%d") for d in frame["Date"]]
                self._frames[symbol] = (frame.reset_index(drop=True), dates)
            except NoMarketDataError as exc:
                logger.warning("Bot: no bars for %s up to %s: %s", symbol, self.as_of, exc)
                self._frames[symbol] = None
            except VendorError as exc:          # the source did not answer: not "no data"
                logger.warning("Bot: bars for %s unavailable: %s", symbol, exc)
                self._frames[symbol] = exc
        return self._frames[symbol]

    def frame(self, symbol: str, day: str, *, required: bool = False):
        """Bars up to and including ``day``, or None. ``required``: a source that did
        not answer raises (the run is retried) instead of passing for "no data"."""
        entry = self._entry(symbol)
        if isinstance(entry, Exception):
            if required:
                raise entry
            return None
        if entry is None:
            return None
        frame, dates = entry
        return frame.iloc[: bisect.bisect_right(dates, day)]

    def sessions(self, symbol: str) -> list[str]:
        entry = self._entry(symbol)
        if isinstance(entry, Exception):
            raise entry
        return list(entry[1]) if entry else []

    def exchange(self, symbol: str) -> str | None:
        """HOSE/HNX/UPCOM (the tick grid of slipped fills); None when unknown (HOSE grid).
        The listing is asked once per run: when it is down, not once per symbol."""
        global _listing_down_until
        if self._boards is None:
            from tradingagents.market import vn_rules

            self._boards = {}
            if time.monotonic() >= _listing_down_until:
                try:
                    from tradingagents.dataflows.vendors import vci

                    self._boards = {str(row.get("symbol", "")).upper(): vn_rules.normalize_exchange(row.get("board"))
                                    for row in vci.fetch_listing() if isinstance(row, dict)}
                except Exception as exc:  # noqa: BLE001 - enrichment only
                    logger.warning("Bot: listing unavailable, HOSE tick grid assumed: %s", exc)
                    _listing_down_until = time.monotonic() + LISTING_RETRY_SECONDS
        return self._boards.get(symbol.upper())

    def bar(self, symbol: str, day: str, *, required: bool = False):
        """The ledger Bar of ``symbol`` on ``day``; None when it did not trade that day."""
        from tradingagents.trading.ledger import Bar

        frame = self.frame(symbol, day, required=required)
        if frame is None or not len(frame) or frame["Date"].iloc[-1].strftime("%Y-%m-%d") != day:
            return None            # suspended (or no data) that session
        row = frame.iloc[-1]
        prev = float(frame["Close"].iloc[-2]) if len(frame) > 1 else None
        prev_date = frame["Date"].iloc[-2].strftime("%Y-%m-%d") if len(frame) > 1 else None
        volume = row.get("Volume")
        volume = float(volume) if _finite(volume) else None
        # NaN prices stay NaN: Bar.tradable then treats the session as one without trading.
        return Bar(day, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]), prev,
                   exchange=self.exchange(symbol), volume=volume, prev_date=prev_date)

    def close(self, symbol: str, day: str) -> float | None:
        frame = self.frame(symbol, day)
        if frame is None or not len(frame):
            return None
        value = float(frame["Close"].iloc[-1])
        return value if _finite(value) else None


class TradingBot:
    def __init__(self, settings: Settings, watchlist, notify=None):
        self.settings, self.watchlist = settings, watchlist
        self.notify = notify or (lambda text: None)
        self.dir = settings.data_dir / "trading"
        self.dir.mkdir(parents=True, exist_ok=True)
        # One run at a time over the paper books and their state files (scheduler,
        # /chay and the web button may all ask at once).
        self._lock = threading.RLock()
        # The real book: written only by /khop and /von, read by the runs. Separate,
        # so a report of a fill is not kept waiting for a whole session run.
        self._real_lock = threading.Lock()
        self._start_guard = threading.Lock()
        self._running = threading.Event()

    # ── books ──
    @property
    def paper_path(self) -> Path:
        return self.dir / "paper.json"

    @property
    def real_path(self) -> Path:
        return self.dir / "real.json"

    @property
    def momentum_path(self) -> Path:
        return self.dir / "paper_momentum.json"

    @property
    def factor_path(self) -> Path:
        return self.dir / "paper_factor.json"

    BOOK_FILES = {"factor": "paper_factor.json", "engine": "paper.json", "momentum": "paper_momentum.json"}
    BOOK_NAMES = {"factor": "Phòng thủ", "engine": "Quant Engine", "momentum": "Động lượng"}
    MIN_CAPITAL, MAX_CAPITAL = 10_000_000, 1_000_000_000_000

    def paper_capital(self) -> float:
        """The starting cash of a new paper book: set on the web page, else PAPER_CAPITAL."""
        try:
            value = float(json.loads((self.dir / "paper_capital.json").read_text(encoding="utf-8"))["capital"])
        except (OSError, ValueError, KeyError, TypeError):
            return self.settings.paper_capital
        return value if self.MIN_CAPITAL <= value <= self.MAX_CAPITAL else self.settings.paper_capital

    def reset_paper(self, capital: float, books: list[str]) -> str:
        """Start the chosen paper books again with ``capital`` (the web page's form).

        A book's history cannot be rescaled honestly (lots, ticks and fees depend on the
        cash), so it starts over: its file is kept as ``<file>.reset-<time>`` and a new book
        trades from the next run (the latest session, no backfill). Refused during a run."""
        chosen = [b for b in books if b in self.BOOK_FILES]
        if not chosen:
            raise ValueError("Chọn ít nhất một tài khoản ảo để đổi vốn.")
        if not self.MIN_CAPITAL <= capital <= self.MAX_CAPITAL:
            raise ValueError(f"Vốn phải từ {_vnd(self.MIN_CAPITAL)} đến {_vnd(self.MAX_CAPITAL)}.")
        if not self._lock.acquire(blocking=False):
            raise ValueError("Bot đang chạy phiên, thử lại sau ít phút.")
        try:
            if self._running.is_set():
                raise ValueError("Bot đang chạy phiên, thử lại sau ít phút.")
            stamp = _now_vn().strftime("%Y%m%d-%H%M%S")
            for book in chosen:
                path = self.dir / self.BOOK_FILES[book]
                if path.exists():
                    path.rename(path.with_name(f"{path.name}.reset-{stamp}"))
            self._write_json("paper_capital.json", {"capital": capital, "at": _now_vn().isoformat(timespec="minutes"),
                                                    "books": chosen})
        finally:
            self._lock.release()
        names = ", ".join(self.BOOK_NAMES[b] for b in chosen)
        logger.info("Paper books %s restarted with %.0f VND", ",".join(chosen), capital)
        return (f"Đã đặt vốn {_vnd(capital)} cho {names}. Tài khoản bắt đầu lại từ phiên chạy tới "
                "(bấm “Chạy phiên ngay” để bắt đầu luôn); sổ cũ được giữ lại trong thư mục data/trading.")

    def factor_book(self):
        from tradingagents.trading.ledger import Ledger

        return Ledger.load(self.factor_path, self.paper_capital(), fresh_on_error=True)

    def momentum_book(self):
        from tradingagents.trading.ledger import Ledger

        # A damaged paper book is moved aside (kept) and restarted.
        return Ledger.load(self.momentum_path, self.paper_capital(), fresh_on_error=True)

    def paper(self):
        from tradingagents.trading.ledger import Ledger

        return Ledger.load(self.paper_path, self.paper_capital(), fresh_on_error=True)

    def real(self):
        """The real book; raises LedgerLoadError when its file is damaged (never emptied).

        The ledger moves a damaged file aside; until its owner restores it (or deletes
        the copy to start over) the book stays refused, rather than coming back empty
        on the next load and being overwritten by the next /von or /khop."""
        from tradingagents.trading.ledger import Ledger, LedgerLoadError

        path = self.real_path
        if not path.exists():
            aside = sorted(path.parent.glob(f"{path.name}.corrupt-*"))
            if aside:
                raise LedgerLoadError(
                    f"Sổ tài khoản thật bị hỏng, đã chuyển sang {aside[-1].name} (không xoá). "
                    f"Khôi phục file đó thành {path.name}, hoặc xoá nó để bắt đầu sổ mới.")
        return Ledger.load(path, 0.0)

    def _real_for_update(self):
        from tradingagents.trading.ledger import LedgerLoadError

        try:
            return self.real()
        except LedgerLoadError as exc:
            raise ValueError(str(exc)) from exc

    def last_signals(self) -> dict:
        try:
            return json.loads((self.dir / "signals.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def last_report(self) -> dict:
        try:
            data = json.loads((self.dir / "last_report.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def books(self) -> list[tuple[str, object]]:
        """(strategy, paper book) of the three strategies, in report order."""
        return [("engine", self.paper()), ("factor", self.factor_book()), ("momentum", self.momentum_book())]

    def last_day(self) -> str:
        """The last session every paper book has processed ("" before the first run)."""
        return min(_book_day(book) for _, book in self.books())

    def _write_json(self, name: str, data) -> None:
        write_text_atomic(self.dir / name, json.dumps(data, ensure_ascii=False))

    # ── the basket ──
    def universe(self) -> list[str]:
        from tradingagents.dataflows import money_flows as mf

        codes: list[str] = []
        if self.settings.universe_vn30:
            codes += mf.index_members("VN30")
        codes += self.watchlist.get()
        out = []
        for code in codes:
            if code not in out:
                out.append(code)
        return out

    # ── the sessions ──
    def run_day(self, day: str) -> str:
        """Bring the books up to ``day`` (every missed session first). Returns the report."""
        return self.catch_up(day).text

    def catch_up(self, day: str) -> RunResult:
        """Process, in order, every final VNINDEX session after each book's last day up to
        ``day``; place the next session's orders; report on the last one."""
        with self._lock:
            self._running.set()
            try:
                return self._catch_up(day)
            finally:
                self._running.clear()

    def _catch_up(self, day: str) -> RunResult:
        from tradingagents.market import vn_rules
        from tradingagents.quant.params import active

        params = active()
        paper, mom_book, fb_book = self.paper(), self.momentum_book(), self.factor_book()
        real, real_error = self._real_snapshot()
        market = _Market(min(day, final_day()))
        sessions = market.sessions(vn_rules.BENCHMARK)     # VNINDEX unreadable: raise, retried later
        if not sessions:
            raise RuntimeError(f"VNINDEX không có phiên nào tới {market.as_of}")
        target = sessions[-1]
        p_todo, p_skipped = _pending(sessions, _book_day(paper))
        m_todo, m_skipped = _pending(sessions, _book_day(mom_book))
        f_todo, f_skipped = _pending(sessions, _book_day(fb_book))
        last = self.last_report()
        if not p_todo and not m_todo and not f_todo:
            if last.get("day") == target and last.get("text"):
                return RunResult(target, last["text"])
            days = [_book_day(paper), _book_day(mom_book), _book_day(fb_book)]
            if any(d != target for d in days):
                return RunResult(target, f"✅ Phiên {target} đã xử lý (sổ đang ở phiên {max(days)}).")
            # Every book is at the target but its report was lost (a crash before writing it):
            # rebuild it from the books, without processing anything again.

        universe = self.universe() if p_todo else None
        engine_out: dict = {}
        for s in p_todo:
            engine_out = self._engine_session(s, sessions, paper, real, market, universe, params, final=s == target)
        fb_decision = None
        for s in f_todo:
            fb_decision = self._factor_session(s, sessions, fb_book, market, params)
        mom_decision = None
        for s in m_todo:
            mom_decision = self._momentum_session(s, sessions, mom_book, market, params)

        if engine_out.get("day") == target:
            signals, real_only, errors = engine_out["signals"], engine_out["real_only"], engine_out["errors"]
        else:
            # The engine book was already at the target (a run resumed after a failure):
            # its decisions were saved; analyse again only if they were not.
            signals, real_only, errors = self._saved_signals(target)
            if signals is None:
                signals, real_only, errors = self._signals_for(
                    target, paper, real, self.universe() if universe is None else universe, params)
        if mom_decision is None:
            mom_decision = self._decision_from_meta(mom_book, params)
        if fb_decision is None:
            fb_decision = self._factor_decision_from_meta(fb_book, params)

        text = self._factor_text(target, fb_book, fb_decision, params)
        text += "\n\n" + self._paper_report(target, paper, [f for f in paper.fills if f.date == target],
                                            [o for o in paper.orders if o.created == target], signals, errors)
        text += "\n\n" + self._momentum_text(target, mom_book, mom_decision, params)
        text += "\n\n" + self._comparison(paper, mom_book, fb_book)
        if self.settings.trade_mode == "signal":
            if real is None:
                text += f"\n\n⚠️ Không đọc được tài khoản thật: {real_error}"
            else:
                real.settle_to(target)               # shares bought T+2 ago are sellable (report only)
                bars = {s: b for s in real.positions if (b := market.bar(s, target)) is not None}
                text += self._strategy_switch_notice(last, real)
                if self.settings.signal_strategy == "factor":
                    text += "\n\n" + self._real_factor_report(target, real, bars, fb_decision, market, params)
                elif self.settings.signal_strategy == "momentum":
                    text += "\n\n" + self._real_momentum_report(target, real, bars, mom_decision, market, params)
                else:
                    text += "\n\n" + self._real_report(target, real, bars, signals, real_only, params)
        done = sorted(set(p_todo) | set(m_todo) | set(f_todo))
        earlier = [s for s in done if s != target]
        head = []
        if earlier:
            head.append(f"⏪ Chạy bù {len(earlier)} phiên bị lỡ: {', '.join(earlier)}"
                        + self._missed_fills(earlier, paper, mom_book, fb_book))
        if p_skipped or m_skipped or f_skipped:
            head.append(f"⚠️ Bỏ qua {max(p_skipped, m_skipped, f_skipped)} phiên cũ hơn (chỉ chạy bù tối đa {MAX_CATCH_UP} "
                        "phiên); lệnh chờ của các phiên đó đã huỷ.")
        if head:
            text = "\n".join(head) + "\n\n" + text
        self._write_json("last_report.json", {"day": target, "text": text,
                                              "at": _now_vn().isoformat(timespec="minutes"),
                                              "sessions": done, "signal_strategy": self.settings.signal_strategy})
        # A rebuilt report (books done, report lost) was never sent either: it is new too.
        return RunResult(target, text, bool(done) or last.get("day") != target, done)

    def _strategy_switch_notice(self, last: dict, real) -> str:
        """A one-time note when the real signals follow another strategy than in the last
        report (a changed SIGNAL_STRATEGY, or the default that moved from momentum to
        factor on 2026-10-15) while the real account holds stocks."""
        from tradingagents.trading.calibrate import STRATEGIES

        now = self.settings.signal_strategy
        if not last.get("day") or not real.positions:
            return ""
        # Reports written before the key existed followed the old default (momentum),
        # unless SIGNAL_STRATEGY said otherwise; that cannot be known, so say "probably".
        before = last.get("signal_strategy")
        if before == now or (before is None and now == "momentum"):
            return ""
        was = STRATEGIES.get(before, before) if before else "có lẽ Động lượng (mặc định cũ)"
        return (f"\n\nℹ️ Tín hiệu thật đổi chiến lược: {was} → {STRATEGIES.get(now, now)}. "
                "Lệnh dưới đây đưa tài khoản thật về rổ của chiến lược mới, kể cả bán các mã đang giữ "
                "theo chiến lược cũ. Muốn giữ chiến lược cũ: đặt SIGNAL_STRATEGY=momentum (hoặc engine) "
                "trong .env rồi khởi động lại bot.")

    def _saved_signals(self, day: str):
        from tradingagents.trading.ledger import Signal

        saved = self.last_signals()
        if saved.get("day") != day or not isinstance(saved.get("signals"), dict):
            return None, {}, []
        try:
            return [Signal(**v) for v in saved["signals"].values()], {}, []
        except TypeError:
            return None, {}, []

    def _real_snapshot(self):
        from tradingagents.trading.ledger import LedgerLoadError

        with self._real_lock:
            try:
                return self.real(), ""
            except LedgerLoadError as exc:
                logger.error("Real book unreadable: %s", exc)
                return None, str(exc)

    def _engine_session(self, day, sessions, paper, real, market, universe, params, *, final) -> dict:
        """One session of the Quant Engine's book: settle, decide, plan, save."""
        from tradingagents.market import vn_rules

        prev = sessions[sessions.index(day) - 1] if sessions.index(day) else None
        _expire(paper, prev)
        touched = {o.symbol for o in paper.orders} | set(paper.positions)
        # A held stock whose source does not answer stops the run (retried later) rather
        # than pass for suspended: its stop would be skipped and its order lost.
        bars = {s: b for s in touched if (b := market.bar(s, day, required=True)) is not None}
        paper.process_session(bars, params=params)
        paper.end_of_session(day, market.close(vn_rules.BENCHMARK, day))
        signals, real_only, errors = self._signals_for(day, paper, real if final else None, universe, params)
        paper.plan_orders(day, signals, max_positions=params.max_positions)
        paper.meta["last_day"] = day
        paper.save(self.paper_path)
        if final:
            self._write_json("signals.json", {"day": day, "signals": {s.symbol: s.__dict__ for s in signals}})
        return {"day": day, "signals": signals, "real_only": real_only, "errors": errors}

    def _signals_for(self, day, paper, real, universe, params):
        """The engine's decisions on ``day`` for the basket, the paper holdings and (when
        ``real`` is given) the real holdings, the latter judged again as holdings."""
        from tradingagents.quant import engine
        from tradingagents.trading.ledger import Signal

        held_real = list(real.positions) if real is not None else []
        symbols = list(dict.fromkeys(list(universe) + list(paper.positions) + held_real))
        signals, real_only, errors, down = [], {}, [], []
        book = paper.portfolio_context()
        for symbol in symbols:
            try:
                result = engine.analyze(symbol, day, portfolio=book, params=params)
                signals.append(Signal.from_result(result))
                if symbol in held_real and symbol not in paper.positions:
                    real_only[symbol] = Signal.from_result(
                        engine.analyze(symbol, day, portfolio=real.portfolio_context(), params=params))
            except Exception as exc:  # noqa: BLE001 - one stock must not stop the session
                errors.append(f"{symbol}: {exc}")
                if _unavailable(exc):
                    down.append(symbol)
                logger.warning("Bot: %s on %s failed: %s", symbol, day, exc)
        held_down = [s for s in down if s in paper.positions or s in held_real]
        if held_down or len(down) > MAX_UNAVAILABLE_SHARE * len(symbols):
            # Raised before the session is saved: the scheduler retries it once the
            # sources answer, instead of a session saved with half its decisions.
            raise RuntimeError(f"Nguồn giá không trả lời cho {len(down)}/{len(symbols)} mã "
                               f"({', '.join(down[:5])}{', ...' if len(down) > 5 else ''}) - phiên {day} "
                               "chưa lưu, sẽ chạy lại.")
        return signals, real_only, errors

    # ── on demand (/chay, the web button) ──
    def session_day(self) -> str:
        """The last final session: today from 15:05, else the previous one (holidays skipped)."""
        from tradingagents.dataflows.ohlcv import load_ohlcv
        from tradingagents.market import vn_rules

        frame = load_ohlcv(vn_rules.BENCHMARK, final_day(), fill_gaps=False)
        return frame["Date"].iloc[-1].strftime("%Y-%m-%d")

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def is_done(self, day: str) -> bool:
        return self.last_day() >= day and self.last_report().get("day") == day

    def start_run(self, notify=None) -> str:
        """Run the last closed session in the background; the report goes to ``notify``."""
        notify = notify or self.notify
        with self._start_guard:
            if self._running.is_set():
                return "⏳ Bot đang chạy một phiên, đợi vài phút."
            try:
                day = self.session_day()
            except Exception as exc:  # noqa: BLE001
                return f"⚠️ Không đọc được dữ liệu VNINDEX: {exc}"
            if self.is_done(day):
                return f"✅ Phiên {day} đã chạy rồi - xem /danhmuc, /lenh hoặc trang /bot."
            self._running.set()

        def work():
            try:
                result = self.catch_up(day)
                notify(result.text if result.new else f"✅ Phiên {day} đã chạy rồi - xem /danhmuc hoặc /lenh.")
            except Exception as exc:  # noqa: BLE001
                logger.exception("On-demand session %s failed", day)
                notify(f"❌ Bot giao dịch lỗi phiên {day}: {exc}")
            finally:
                self._running.clear()

        threading.Thread(target=work, name="bot-run-now", daemon=True).start()
        return f"▶️ Đang chạy phiên {day} cho ba tài khoản ảo (khoảng 1 phút); báo cáo sẽ gửi khi xong."

    # ── the defensive book (the default strategy) ──
    def _factor_session(self, day, sessions, book, market, params):
        """One session of the defensive book: settle, check the market, rebuild when due,
        plan, save. Returns the decision."""
        from tradingagents.market import vn_rules
        from tradingagents.trading import factor

        i = sessions.index(day)
        prev = sessions[i - 1] if i else None
        _expire(book, prev)
        touched = {o.symbol for o in book.orders} | set(book.positions)
        bars = {s: b for s in touched if (b := market.bar(s, day, required=True)) is not None}
        book.process_session(bars, rules=factor.RULES)
        book.end_of_session(day, market.close(vn_rules.BENCHMARK, day))
        # The session after ``day`` is known when it is replayed; the latest one's is guessed
        # (a wrong guess makes the week's check or the month's rebuild run one session late).
        next_day = sessions[i + 1] if i + 1 < len(sessions) else factor.next_session_guess(day)
        vni = market.frame(vn_rules.BENCHMARK, day)
        vni_closes = [float(c) for c in vni["Close"].tail(max(params.fb_market_ma, 1))] if vni is not None else []
        decision = factor.decide(book, day, next_day, vni_closes, params,
                                 lambda: self._factor_ranking(day, sessions[: i + 1], market, params))
        last_close = {}
        for symbol in set(decision.basket) | set(book.positions):
            bar = bars.get(symbol) or market.bar(symbol, day, required=symbol in decision.basket)
            if bar is not None and _finite(bar.close) and bar.close > 0:
                last_close[symbol] = float(bar.close)
        factor.plan(book, day, decision, last_close, params)
        book.meta["last_day"] = day
        book.save(self.factor_path)
        return decision

    def _factor_ranking(self, day, sessions, market, params):
        """The defensive book's ranking on ``day`` (``sessions``: VNINDEX sessions up to it)."""
        from tradingagents.trading import factor

        universe = self._factor_universe(day, sessions, market, params)
        names = []
        for symbol in universe:
            bar = market.bar(symbol, day, required=True)     # a source down: the session is retried
            if bar is not None and _finite(bar.close) and bar.close > 0:
                names.append(symbol)
        window = sessions[-factor.SCORE_WINDOW:]
        closes = {}
        for symbol in names:
            frame = market.frame(symbol, day)
            series = {d.strftime("%Y-%m-%d"): float(c) for d, c in zip(frame["Date"], frame["Close"], strict=True)}
            closes[symbol] = factor.aligned_closes(series, window)
        return factor.rank(closes, names)

    @property
    def universe_path(self) -> Path:
        return self.dir / UNIVERSE_FILE

    def _read_universes(self) -> dict:
        try:
            data = json.loads(self.universe_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        days = data.get("days") if isinstance(data, dict) else None
        return {d: e for d, e in (days or {}).items() if isinstance(e, dict) and isinstance(e.get("universe"), list)}

    def _factor_universe(self, day, sessions, market, params) -> list[str]:
        """The cap universe on ``day``: built from the vendors once per session and cached.

        When it cannot be built the session fails and is retried later, as for a
        missing bar: a basket ranked on another universe (an old build, today's
        VN30) would be held for a month on rules that were never evaluated."""
        cache = self._read_universes()
        entry = cache.get(day)
        if entry and entry.get("size") == params.fb_universe:
            return list(entry["universe"])
        try:
            built = self._build_universe(day, sessions, market, params)
        except Exception as exc:
            logger.warning("Bot: cap universe for %s unavailable: %s", day, exc)
            raise RuntimeError(f"chưa dựng được rổ vốn hoá ngày {day} cho Phòng thủ ({exc}); "
                               "phiên sẽ được thử lại") from exc
        cache[day] = {"size": params.fb_universe, "universe": built["universe"], "pool": built["pool"],
                      "at": _now_vn().isoformat(timespec="minutes")}
        keep = dict(sorted(cache.items())[-UNIVERSE_KEEP:])
        self._write_json(UNIVERSE_FILE, {"days": keep})
        return list(built["universe"])

    def _build_universe(self, day, sessions, market, params) -> dict:
        from tradingagents.dataflows import money_flows as mf
        from tradingagents.dataflows.vendors import vci
        from tradingagents.market import vn_rules
        from tradingagents.trading import factor

        hose = [str(row.get("symbol", "")).upper() for row in vci.fetch_listing()
                if isinstance(row, dict) and str(row.get("type", "")).upper() == "STOCK"
                and vn_rules.normalize_exchange(row.get("board")) == "HOSE"]

        def bars_of(symbol):
            frame = market.frame(symbol, day, required=True)
            if frame is None:
                return None
            volume = frame["Volume"] if "Volume" in frame else [math.nan] * len(frame)
            return {d.strftime("%Y-%m-%d"): (float(c), float(v) if _finite(v) else math.nan)
                    for d, c, v in zip(frame["Date"], frame["Close"], volume, strict=True)}

        def history_of(symbol):
            frame = mf.load_price_history(symbol, day, guarded=False)     # build_universe judges outages
            if frame is None or frame.empty:
                return {}
            cols = [frame[c] if c in frame else [math.nan] * len(frame) for c in ("MatchValue", "Close", "TotalShares")]
            return {d.strftime("%Y-%m-%d"): (float(a), float(b), float(c))
                    for d, a, b, c in zip(frame["Date"], *cols, strict=True)}

        return factor.build_universe(sessions, hose, params.fb_universe, bars_of=bars_of, history_of=history_of)

    @staticmethod
    def _factor_decision_from_meta(book, params):
        """The decision of the book's last session, from the state ``factor.decide`` kept."""
        from tradingagents.trading.factor import Decision

        on = bool(book.meta.get("fb_on", False))
        note = "" if on else f"VNINDEX dưới MA{params.fb_market_ma} - giữ tiền mặt"
        return Decision(on, False, list(book.meta.get("fb_basket") or []), note=note)

    def _factor_text(self, day, book, decision, params) -> str:
        fills = [f for f in book.fills if f.date == day]
        orders = [o for o in book.orders if o.created == day]
        st = book.stats()
        bench_txt = f" | VNINDEX {st['benchmark_pct']:+.2f}%" if st["benchmark_pct"] is not None else ""
        state = "trên" if decision.market_ok else "dưới"
        lines = [f"🛡️ BOT PHÒNG THỦ {day}",
                 f"NAV {_fmt(st['nav'])} ({st['return_pct']:+.2f}% từ {book.start_date or day}){bench_txt} | "
                 f"sụt giảm tối đa {st['max_drawdown_pct']:.2f}%",
                 f"Thị trường: VNINDEX {state} MA{params.fb_market_ma} (xét phiên cuối tuần) | rổ: "
                 + (", ".join(decision.basket) or "tiền mặt")
                 + (f" ({decision.note})" if decision.note and (decision.rebalanced or not decision.market_ok) else "")]
        lines += _adjustment_lines(book, day)
        if fills:
            lines += [f"- Khớp {f.side} {f.symbol} {_fmt(f.qty)} @ {_fmt(f.price)} ({f.reason})"
                      + (f", lãi/lỗ {_bn(f.pnl)}" if f.pnl is not None else "") for f in fills]
        if orders:
            lines += [f"- Lệnh phiên tới: {'MUA' if o.side == 'BUY' else 'BÁN'} {o.symbol} {_fmt(o.qty)} giá mở cửa ({o.reason})"
                      for o in orders]
        return "\n".join(lines)

    def _real_factor_report(self, day, real, bars, decision, market, params) -> str:
        from tradingagents.trading import factor

        return self._real_rotation_report(day, real, bars, decision, market, params, factor.plan,
                                          "📣 TÍN HIỆU THẬT - PHÒNG THỦ (tự đặt lệnh, rồi báo /khop)")

    # ── the momentum book ──
    def _momentum_session(self, day, sessions, book, market, params):
        """One session of the momentum book: settle, rank, plan, save. Returns the decision."""
        from tradingagents.dataflows import money_flows as mf
        from tradingagents.market import vn_rules
        from tradingagents.trading import momentum

        prev = sessions[sessions.index(day) - 1] if sessions.index(day) else None
        _expire(book, prev)
        touched = {o.symbol for o in book.orders} | set(book.positions)
        bars = {s: b for s in touched if (b := market.bar(s, day, required=True)) is not None}
        book.process_session(bars, rules=momentum.RULES)
        book.end_of_session(day, market.close(vn_rules.BENCHMARK, day))
        need = params.mom_lookback + 1
        closes = {}
        for symbol in mf.index_members("VN30"):
            frame = market.frame(symbol, day)
            if frame is None or not len(frame) or frame["Date"].iloc[-1].strftime("%Y-%m-%d") != day:
                continue
            closes[symbol] = [float(c) for c in frame["Close"].tail(need)]
        vni = market.frame(vn_rules.BENCHMARK, day)
        vni_closes = [float(c) for c in vni["Close"].tail(params.mom_market_ma)] if vni is not None else []
        decision = momentum.decide(book, closes, vni_closes, params)
        momentum.plan(book, day, decision, {s: c[-1] for s, c in closes.items()}, params)
        book.meta["last_day"] = day
        book.save(self.momentum_path)
        return decision

    @staticmethod
    def _decision_from_meta(book, params):
        """The decision of the book's last session, from the state ``momentum.decide`` kept."""
        from tradingagents.trading.momentum import Decision

        meta = book.meta
        ok = bool(meta.get("mom_market_ok", False))
        note = "" if ok else f"VNINDEX dưới MA{params.mom_market_ma} - giữ tiền mặt"
        return Decision(ok, int(meta.get("mom_since", 1) or 0) == 0 and ok, list(meta.get("mom_basket") or []),
                        note=note)

    def _momentum_text(self, day, book, decision, params) -> str:
        fills = [f for f in book.fills if f.date == day]
        orders = [o for o in book.orders if o.created == day]
        st = book.stats()
        bench_txt = f" | VNINDEX {st['benchmark_pct']:+.2f}%" if st["benchmark_pct"] is not None else ""
        lines = [f"🚀 BOT ĐỘNG LƯỢNG {day}",
                 f"NAV {_fmt(st['nav'])} ({st['return_pct']:+.2f}% từ {book.start_date or day}){bench_txt} | "
                 f"sụt giảm tối đa {st['max_drawdown_pct']:.2f}%",
                 ("Thị trường: VNINDEX trên MA" if decision.market_ok else "Thị trường: VNINDEX dưới MA")
                 + f"{params.mom_market_ma} | rổ: " + (", ".join(decision.basket) or "tiền mặt")
                 + (f" ({decision.note})" if decision.note and (decision.rebalanced or not decision.market_ok) else "")]
        lines += _adjustment_lines(book, day)
        if fills:
            lines += [f"- Khớp {f.side} {f.symbol} {_fmt(f.qty)} @ {_fmt(f.price)} ({f.reason})"
                      + (f", lãi/lỗ {_bn(f.pnl)}" if f.pnl is not None else "") for f in fills]
        if orders:
            lines += [f"- Lệnh phiên tới: {'MUA' if o.side == 'BUY' else 'BÁN'} {o.symbol} {_fmt(o.qty)} giá mở cửa ({o.reason})"
                      for o in orders]
        return "\n".join(lines)

    @staticmethod
    def _missed_fills(days, paper, mom_book, fb_book=None) -> str:
        wanted = set(days)
        lines = []
        books = [("Phòng thủ", fb_book)] if fb_book is not None else []
        for title, book in books + [("Quant Engine", paper), ("Động lượng", mom_book)]:
            lines += [f"\n- {f.date} {title}: {f.side} {f.symbol} {_fmt(f.qty)} @ {_fmt(f.price)} ({f.reason})"
                      for f in book.fills if f.date in wanted]
        return "".join(lines) if lines else " (không có lệnh khớp)"

    @staticmethod
    def _comparison(engine_book, mom_book, fb_book=None) -> str:
        """Each book's return since its own start (dated: the books may have started on
        different days, and then the figures cover different periods)."""
        books = ([("Phòng thủ", fb_book)] if fb_book is not None else []) + [
            ("Quant Engine", engine_book), ("Động lượng", mom_book)]
        parts, starts = [], set()
        for title, book in books:
            st = book.stats()
            since = f" từ {book.start_date}" if book.start_date else ""
            starts.add(book.start_date or "")
            parts.append(f"{title} {st['return_pct']:+.2f}%{since} (DD {st['max_drawdown_pct']:.1f}%)")
        text = "📊 So sánh: " + " | ".join(parts)
        if len(starts) > 1:
            text += "\n(các tài khoản bắt đầu ở các ngày khác nhau: lợi nhuận không cùng giai đoạn)"
        return text

    def _real_momentum_report(self, day, real, bars, decision, market, params) -> str:
        from tradingagents.trading import momentum

        return self._real_rotation_report(day, real, bars, decision, market, params, momentum.plan,
                                          "📣 TÍN HIỆU THẬT - ĐỘNG LƯỢNG (tự đặt lệnh, rồi báo /khop)")

    def _real_rotation_report(self, day, real, bars, decision, market, params, planner, title) -> str:
        """Orders that move the real book to a rotation book's basket (``planner``: its ``plan``)."""
        from tradingagents.trading.ledger import Ledger

        lines = [title]
        for symbol, pos in real.positions.items():
            bar = bars.get(symbol)
            if bar is not None and bar.tradable and pos.stop and bar.low <= pos.stop:
                lines.append(f"- ⛔ {symbol}: chạm stop {_fmt(pos.stop)} - {_sell_advice(pos, day)}")
        if real.cash <= 0 and not real.positions:
            lines.append("Chưa có vốn tài khoản thật: gửi /von 500000000 để bot tính khối lượng.")
            return "\n".join(lines)
        draft = Ledger.from_dict(copy.deepcopy(real.to_dict()))
        draft.orders = []
        last = {}
        for symbol in set(decision.basket) | set(real.positions):
            close = market.close(symbol, day)
            if close is not None:
                last[symbol] = close
        orders = planner(draft, day, decision, last, params)
        for o in orders:
            pos = real.positions.get(o.symbol)
            if o.side == "SELL" and pos is not None and _pending_lots(pos, day):
                lines.append(f"- BÁN {o.symbol}: {_sell_advice(pos, day, when=' giá mở cửa')} ({o.reason})")
            else:
                lines.append(f"- {'MUA' if o.side == 'BUY' else 'BÁN'} {o.symbol} {_fmt(o.qty)} cp giá mở cửa ({o.reason})")
        if len(lines) == 1:
            lines.append("Không đổi danh mục phiên tới.")
        return "\n".join(lines)

    def _paper_report(self, day, paper, fills, orders, signals, errors) -> str:
        s = paper.stats()
        bench = f" | VNINDEX {s['benchmark_pct']:+.2f}%" if s["benchmark_pct"] is not None else ""
        lines = [f"🤖 BOT PAPER {day}",
                 f"NAV {_fmt(s['nav'])} ({s['return_pct']:+.2f}% từ {paper.start_date or day}){bench} | "
                 f"sụt giảm tối đa {s['max_drawdown_pct']:.2f}%"]
        if fills:
            lines.append("Khớp hôm nay:")
            lines += [f"- {f.side} {f.symbol} {_fmt(f.qty)} @ {_fmt(f.price)} ({f.reason})"
                      + (f", lãi/lỗ {_bn(f.pnl)}" if f.pnl is not None else "") for f in fills]
        lines += _adjustment_lines(paper, day)
        if paper.positions:
            lines.append("Đang giữ:")
            for p in paper.positions.values():
                pnl = (p.last_price / p.avg_price - 1) * 100 if p.avg_price else 0
                lines.append(f"- {p.symbol} {_fmt(p.qty)} cp, vốn {_fmt(p.avg_price)}, giá {_fmt(p.last_price)} "
                             f"({pnl:+.1f}%), stop {_fmt(p.stop)}, T1 {_fmt(p.target1)}"
                             + ("" if p.sellable else " [chưa về T+2]"))
        if orders:
            lines.append("Lệnh cho phiên tới:")
            lines += [f"- {o.side} {o.symbol} {_fmt(o.qty)}"
                      + (f" giá ≤ {_fmt(o.limit)}, stop {_fmt(o.stop)}, T1 {_fmt(o.target1)}" if o.limit else " giá mở cửa")
                      for o in orders]
        else:
            buys = [s for s in signals if s.action == "BUY"]
            lines.append("Không có lệnh mới." + (f" ({len(buys)} tín hiệu BUY nhưng hết chỗ/tiền)" if buys else
                                                  " Không mã nào đạt điều kiện BUY."))
        if errors:
            lines.append(f"⚠️ {len(errors)} mã lỗi dữ liệu: " + "; ".join(errors[:3]))
        return "\n".join(lines)

    def _real_report(self, day, real, bars, signals, real_only, params) -> str:
        from tradingagents.trading.ledger import Ledger

        lines = ["📣 TÍN HIỆU CHO TÀI KHOẢN THẬT (tự đặt lệnh, rồi báo /khop)"]
        for symbol, pos in real.positions.items():
            bar = bars.get(symbol)
            if bar is not None and not bar.tradable:
                bar = None
            sig = real_only.get(symbol) or next((s for s in signals if s.symbol == symbol), None)
            pending = _pending_lots(pos, day)
            if bar is not None and pos.stop and bar.low <= pos.stop:
                lines.append(f"- ⛔ {symbol}: chạm stop {_fmt(pos.stop)} hôm nay - {_sell_advice(pos, day)}")
            elif bar is not None and pos.target1 and not pos.t1_done and bar.high > pos.target1:
                # Traded THROUGH T1, as the ledger's resting limit needs (``_target_fill``).
                lines.append(f"- 🎯 {symbol}: chạm T1 {_fmt(pos.target1)} - đề xuất bán 1/2, dời stop về hoà vốn"
                             + (f" ({_t2_note(pending, day)})" if pending else ""))
            elif sig is not None and sig.action == "SELL":
                lines.append(f"- 🔻 {symbol}: hệ thống chuyển SELL - {_sell_advice(pos, day, verb='bán', when=' phiên tới')}")
        if real.cash <= 0 and not real.positions:
            lines.append("Chưa có vốn tài khoản thật: gửi /von 500000000 để bot tính khối lượng.")
            return "\n".join(lines)
        draft = Ledger.from_dict(copy.deepcopy(real.to_dict()))
        draft.orders = []
        orders = draft.plan_orders(day, signals, max_positions=params.max_positions)
        buys = [o for o in orders if o.side == "BUY"]
        if buys:
            lines += [f"- MUA {o.symbol} {_fmt(o.qty)} cp, giá ≤ {_fmt(o.limit)}, stop {_fmt(o.stop)}, "
                      f"T1 {_fmt(o.target1)}" + (f", T2 {_fmt(o.target2)}" if o.target2 else "") for o in buys]
        elif len(lines) == 1:
            lines.append("Không có lệnh đề xuất cho phiên tới.")
        return "\n".join(lines)

    # ── the real book, updated by its owner ──
    @staticmethod
    def _fill_day(day: str | None, last: str) -> str:
        """The fill's session: default today (Vietnam time); a weekday, not in the future,
        not before the last fill already recorded (the book is kept in date order)."""
        raw = (day or "").strip() or today_vn()
        try:
            parsed = _date.fromisoformat(raw[:10]) if len(raw) == 10 else None
        except ValueError:
            parsed = None
        if parsed is None:
            raise ValueError(f"Ngày khớp không hợp lệ: {raw!r}. Dùng dạng YYYY-MM-DD.")
        text = parsed.isoformat()
        if text > today_vn():
            raise ValueError(f"Ngày khớp {text} ở tương lai (hôm nay {today_vn()}).")
        if parsed.weekday() >= 5:
            raise ValueError(f"{text} là cuối tuần, không có phiên giao dịch.")
        if last and text < last:
            raise ValueError(f"Ngày khớp {text} trước lệnh đã ghi gần nhất ({last}); "
                             "báo các lệnh theo đúng thứ tự ngày.")
        return text

    def record_fill(self, side: str, symbol: str, qty: int, price: float, day: str | None = None) -> str:
        """Book a fill the owner made at the broker (/khop). Raises ValueError with a
        Vietnamese message, leaving the book untouched, when it cannot have happened."""
        from tradingagents.market import vn_rules

        side = {"MUA": "BUY", "BAN": "SELL", "BÁN": "SELL"}.get(str(side).strip().upper(), str(side).strip().upper())
        if side not in ("BUY", "SELL"):
            raise ValueError("Dùng: /khop mua|ban MÃ KHỐI_LƯỢNG GIÁ [YYYY-MM-DD]")
        symbol = str(symbol).strip().upper()
        if isinstance(qty, bool) or not _finite(qty) or float(qty) != int(float(qty)) or int(float(qty)) <= 0 \
                or int(float(qty)) % vn_rules.LOT_SIZE:
            raise ValueError(f"Khối lượng phải là bội số dương của {vn_rules.LOT_SIZE} cổ phiếu.")
        qty = int(float(qty))
        if not _finite(price) or float(price) <= 0:
            raise ValueError("Giá khớp không hợp lệ.")
        price = float(price)
        with self._real_lock:
            real = self._real_for_update()
            day = self._fill_day(day, str(real.meta.get("last_day") or ""))
            stop = target1 = target2 = None
            if side == "BUY":
                if self.settings.signal_strategy == "factor":
                    pass                        # held until it leaves the basket: no stop, no target
                elif self.settings.signal_strategy == "momentum":
                    from tradingagents.quant.params import active

                    pct = active().mom_stop_pct
                    stop = vn_rules.round_to_tick(price * (1 - pct / 100)) if pct else None
                else:
                    sig = (self.last_signals().get("signals") or {}).get(symbol) or {}
                    stop = float(sig["stop"]) if _finite(sig.get("stop")) and sig["stop"] > 0 else None
                    target1 = float(sig["target1"]) if _finite(sig.get("target1")) else None
                    target2 = float(sig["target2"]) if _finite(sig.get("target2")) else None
            held = real.positions.get(symbol)
            t1_before = bool(held and held.t1_done)
            fill = real.record_fill(side, symbol, qty, price, day, stop=stop, target1=target1, target2=target2,
                                    reason="báo khớp")
            real.meta["last_day"] = max(day, str(real.meta.get("last_day") or ""))
            real.save(self.real_path)
        if side == "BUY":
            pos = real.positions[symbol]
            if self.settings.signal_strategy == "factor":
                note = "giữ tới khi rời rổ phòng thủ (hoặc VNINDEX dưới MA)"
            elif self.settings.signal_strategy == "momentum":
                note = f"stop {_fmt(pos.stop)}" if pos.stop else "giữ tới khi rời rổ động lượng"
            else:
                note = (f"stop {_fmt(pos.stop)}, T1 {_fmt(pos.target1)}" if pos.stop
                        else "chưa có stop (không có tín hiệu BUY gần nhất)")
            return (f"✅ Đã ghi MUA {symbol} {_fmt(qty)} @ {_fmt(price)} ngày {day}; {note}. "
                    f"Bán được từ chiều T+2. Tiền còn {_fmt(real.cash)}.")
        text = f"✅ Đã ghi BÁN {symbol} {_fmt(qty)} @ {_fmt(price)} ngày {day}; lãi/lỗ {_bn(fill.pnl)}."
        pos = real.positions.get(symbol)
        if pos is not None and pos.t1_done and not t1_before:
            text += f" Đã chốt T1: stop phần còn lại ({_fmt(pos.qty)} cp) dời về hoà vốn {_fmt(pos.stop)}."
        return text

    def set_capital(self, cash: float) -> str:
        """Set the real book's cash (/von), as the broker shows it."""
        if isinstance(cash, bool) or not _finite(cash) or float(cash) <= 0:
            raise ValueError("Vốn phải là một số lớn hơn 0.")
        if float(cash) > 1e13:
            raise ValueError("Số vốn quá lớn - kiểm tra lại (nhập theo đồng, ví dụ 500000000).")
        cash = float(cash)
        with self._real_lock:
            real = self._real_for_update()
            real.cash = cash
            if not real.positions:
                real.start_cash = cash
            real.save(self.real_path)
        return f"✅ Tiền mặt tài khoản thật: {_fmt(cash)} VND."

    # ── status ──
    def portfolio_text(self) -> str:
        from tradingagents.trading.ledger import LedgerLoadError

        paper = self.paper()
        s = paper.stats()
        lines = [f"🤖 Paper: NAV {_fmt(s['nav'])} ({s['return_pct']:+.2f}%), tiền {_fmt(paper.cash)}, "
                 f"{s['trades']} lệnh mua, thắng {s['win_rate_pct'] if s['win_rate_pct'] is not None else '-'}%"]
        lines += [f"- {p.symbol} {_fmt(p.qty)} @ {_fmt(p.avg_price)} → {_fmt(p.last_price)}, stop {_fmt(p.stop)}"
                  for p in paper.positions.values()] or ["- (không giữ mã nào)"]
        fb = self.factor_book()
        f = fb.stats()
        lines.append(f"🛡️ Phòng thủ: NAV {_fmt(f['nav'])} ({f['return_pct']:+.2f}%), tiền {_fmt(fb.cash)}, "
                     f"rổ {', '.join(fb.meta.get('fb_basket') or []) or 'tiền mặt'}")
        lines += [f"- {p.symbol} {_fmt(p.qty)} @ {_fmt(p.avg_price)} → {_fmt(p.last_price)}"
                  for p in fb.positions.values()] or ["- (không giữ mã nào)"]
        mom = self.momentum_book()
        m = mom.stats()
        lines.append(f"🚀 Động lượng: NAV {_fmt(m['nav'])} ({m['return_pct']:+.2f}%), tiền {_fmt(mom.cash)}, "
                     f"rổ {', '.join(mom.meta.get('mom_basket') or []) or 'tiền mặt'}")
        lines += [f"- {p.symbol} {_fmt(p.qty)} @ {_fmt(p.avg_price)} → {_fmt(p.last_price)}"
                  for p in mom.positions.values()] or ["- (không giữ mã nào)"]
        if self.settings.trade_mode == "signal":
            try:
                real = self.real()
            except LedgerLoadError as exc:
                lines.append(f"💼 Thật: ⚠️ {exc}")
                return "\n".join(lines)
            real.settle_to(today_vn())
            lines.append(f"💼 Thật: tiền {_fmt(real.cash)}")
            lines += [f"- {p.symbol} {_fmt(p.qty)} @ {_fmt(p.avg_price)}, stop {_fmt(p.stop)}, T1 {_fmt(p.target1)}"
                      + ("" if p.settled_qty >= p.qty else f" [{_fmt(p.settled_qty)} cp đã về]")
                      for p in real.positions.values()] or ["- (chưa ghi vị thế nào)"]
        return "\n".join(lines)

    def orders_text(self) -> str:
        lines = []
        for title, book in (("Phòng thủ", self.factor_book()), ("Quant Engine", self.paper()),
                            ("Động lượng", self.momentum_book())):
            lines.append(f"{title}:")
            lines += [f"- {o.side} {o.symbol} {_fmt(o.qty)}"
                      + (f" giá ≤ {_fmt(o.limit)}, stop {_fmt(o.stop)}" if o.limit else " giá mở cửa")
                      for o in book.orders] or ["- không có lệnh chờ"]
        return "Lệnh cho phiên tới\n" + "\n".join(lines)

    def calibration_text(self) -> str:
        from tradingagents.quant import params as qp
        from tradingagents.trading import calibrate as cal

        current = qp.active()
        diff = qp.changed(current)
        lines = ["⚙️ Ngưỡng đang dùng: " + (", ".join(f"{k} {a}→{b}" for k, (a, b) in diff.items())
                                         if diff else "mặc định (chưa đổi lần nào)")]
        reports = cal.last_reports(10)
        for row in reversed(reports):
            acc = row.get("accepted")
            who = cal.STRATEGIES.get(row.get("strategy", "engine"), "")
            lines.append(f"- {row['as_of']} {who}: " + (f"ĐỔI {acc['name']}" if acc else f"giữ nguyên. {row.get('note', '')}"))
        if not reports:
            lines.append("Chưa chạy hiệu chỉnh lần nào.")
        return "\n".join(lines)

    # ── nightly ──
    def calibrate(self, day: str) -> str:
        """Calibrate on the sessions up to ``day``, which must be a final one (never past
        the last session the books processed)."""
        from tradingagents.dataflows import money_flows as mf
        from tradingagents.trading import calibrate as cal, simulator as sim

        day = min(day, final_day())
        symbols = list(dict.fromkeys(self.universe() + mf.index_members("VN30")))
        data = sim.build_dataset(symbols, cal.TRAIN_SESSIONS + cal.HOLDOUT_SESSIONS, day)
        out = []
        for strategy in ("engine", "momentum"):
            if strategy == "momentum":
                members = set(mf.index_members("VN30"))
                data.symbols = [s for s in data.symbols if s in members]
                data.bars = {s: b for s, b in data.bars.items() if s in members}
            out.append("⚙️ " + cal.calibrate(symbols, day, apply=True, data=data, strategy=strategy).summary())
        # The defensive book is not calibrated: its parameters stay the ones the
        # evaluation froze and tested (a recalibrating variant was never evaluated, and
        # a dataset of today's names cannot rebuild its point-in-time universe).
        return "\n\n".join(out)
