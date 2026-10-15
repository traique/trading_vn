"""The defensive trend book, the bot's default strategy since 2026-10-15.

A 10-stock, low-turnover book of large caps, held only while the market trends
up. Decisions are taken at the close of session D; every order goes to D+1's
open through the same ledger as the other books (lots of 100, fees and sell
tax, slippage, no fill on a limit-locked session, T+2).

1. Universe on D (``cap_universe``): HOSE stocks that have at least 250
   sessions of bars, an unadjusted close of at least 5,000đ and a bar in one of
   the last 5 sessions; the 100 with the highest median matched value over the
   last 60 sessions (a session without a bar counts as 0); of those, the
   ``fb_universe`` (50) largest by market cap = shares outstanding (VCI
   price-history) x unadjusted close on D. Only names with a bar on D are scored.
2. Score (``rank``): over the last 252 VNINDEX sessions of adjusted closes,
   vol = stdev of the daily log returns (at least 150 needed) and hi = close on
   D / the highest close of the window; score = rank(-vol) + rank(hi) within
   the universe (average ranks, higher is better). Ties go to the lower vol.
3. Basket of ``fb_top_k`` (10) (``select``): held names still ranked in the top
   ``fb_buffer`` x K (15) stay; the other slots go to the best-ranked names.
   A new name is bought with 0.98 / K of NAV; held names are never resized; a
   name that leaves the basket is sold in full.
4. Calendar (``decide``): the basket is rebuilt on the last session of each
   month (every ``fb_rebalance_months``-th month) and whenever the market filter
   turns on; between rebuilds the same basket is sent again every session, so
   a blocked buy or sell (limit lock, suspension, T+2) is retried.
5. Market filter: invested only while VNINDEX closes above its
   ``fb_market_ma`` (200)-session average, checked on the last session of each
   ISO week (and on a book's first session). When it is off everything is sold
   and the book waits in cash until a weekly check turns it on again.
6. No stop, no target, no time stop (``RULES``).

Live, the session after D is known when D is replayed; for the latest session
it is guessed from the exchange calendar (``vn_rules.is_session_day``: weekends,
the fixed-date holidays and the dated table of Tết, Hùng Kings, days in lieu
and bridge days). If the guess was wrong (a closure the table does not know),
the week's check or the month's rebuild that was due runs late, on the first
session after it.

Independent evaluation of 2026-10-15 (research/eval; data to 2026-10-14,
VCI adjusted bars, point-in-time universe of 568 HOSE/ex-HOSE stocks, 162 of
which left HOSE (delisted, or moved to HNX/UPCoM); the repo's ledger with 0.15% buy fee, 0.15% sell fee + 0.1% tax,
0.1% slippage per side, next-open fills, T+2, lots, ticks, bands; 1e9 VND;
cash earns 0). CAGR / max drawdown / Sharpe, after costs:

    TRAIN 2013-2020           18.1% / -25.9% / 1.37   (VNINDEX 12.9% / -45.3%)
    VALID 2021-2023           13.9% / -12.0% / 0.99   (VNINDEX  0.3% / -40.3%)
    TEST  2024-01..2026-10     8.1% / -20.8% / 0.55   (VNINDEX 15.9% / -18.1%)
    2013-01..2026-10 (one run) 15.6% / -25.9% / 1.11  (VNINDEX 10.7% / -45.3%)

These are the research harness's numbers. This module breaks equal scores by
the lower volatility, where the harness left them to pandas' unstable sort
order; scores are sums of ranks, so ties at the K and buffer cut-offs happen
and the two books part ways from time to time. Replayed through this module on
the same data: TRAIN 17.7%, VALID 13.9%, TEST 8.4%, 2013-2026 15.3% / -26.4%.

TEST was run once, after the selection rule was frozen. The edge is risk, not
return: against the old VN30 momentum rule run on a point-in-time VN30 proxy
it made 15.6% vs 4.7% a year over 2013-2026, but it trailed it on TEST (8.1%
vs 11.8%, not significant) and it lagged VNINDEX in the 2024-2026 rally. The
cap ranking can only see stocks with VCI share counts: 7 delisted names never
enter it (a small optimistic bias). VNINDEX is a price index (no dividends).
Past results do not promise future ones.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date as _date, timedelta

import numpy as np

from tradingagents.market import vn_rules
from tradingagents.quant.params import Params
from tradingagents.trading.ledger import Ledger, Order, _ExitRules

# Held until the basket drops it: no T1/T2, no time stop.
RULES = _ExitRules(max_hold_sessions=10**9, t1_sell_fraction=0.0, use_t2=False, trail_pct=0.0)

INVEST = 0.98              # share of NAV the full basket is bought with (room for gaps and fees)
LIQUID_POOL = 100          # the liquidity screen the cap ranking is taken from
MIN_HISTORY = 250          # sessions of bars a stock needs to enter the universe
MIN_PRICE = 5_000.0        # unadjusted close, VND
LIQ_WINDOW = 60            # sessions of matched value for the median
LIQ_MIN_PERIODS = LIQ_WINDOW // 2
RECENT_SESSIONS = 5        # a bar in one of these is required
CAP_FFILL = 5              # sessions a market cap is carried forward
SCORE_WINDOW = 252         # sessions of closes the score reads (~1 year)
MIN_RETURNS = 150          # valid daily returns needed for a volatility
# Fixed-date market holidays (kept here for older callers; see vn_rules).
FIXED_HOLIDAYS = vn_rules.FIXED_HOLIDAYS
# Universe build: this many vendor failures in a row is an outage (stop asking).
MAX_FAILED_IN_A_ROW = 10


@dataclass
class Snapshot:
    """One stock on session D, what the universe needs."""

    symbol: str
    hose: bool                       # listed on HOSE
    bars: int                        # sessions with a bar up to D
    raw_close: float | None          # last unadjusted close up to D
    recent: bool                     # a bar in one of the last RECENT_SESSIONS sessions
    median_value: float | None       # median matched value of the last LIQ_WINDOW sessions
    market_cap: float | None         # shares x unadjusted close on D
    has_bar: bool                    # traded on D

    @property
    def eligible(self) -> bool:
        return (self.hose and self.bars >= MIN_HISTORY and self.raw_close is not None
                and self.raw_close >= MIN_PRICE and self.recent
                and self.median_value is not None and self.median_value > 0)


@dataclass
class Ranked:
    symbol: str
    score: float
    vol: float                       # stdev of daily log returns
    hi: float                        # close / highest close of the window


@dataclass
class Decision:
    market_ok: bool
    rebalanced: bool
    basket: list[str]
    checked: bool = False            # the market filter was evaluated this session
    ranking: list[Ranked] = field(default_factory=list)
    note: str = ""


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


# ── the universe ─────────────────────────────────────────────────────────────


def snapshot(symbol: str, sessions: list[str], bars: Mapping[str, tuple[float, float]],
             history: Mapping[str, tuple[float, float, float]] | None = None, *, hose: bool = True) -> Snapshot:
    """A stock on the last of ``sessions`` (VNINDEX sessions, oldest first, up to D).

    ``bars``: {day: (adjusted close, volume)} up to D. ``history``: VCI
    price-history {day: (matched value, unadjusted close, shares outstanding)}
    (NaN where missing). Matched value falls back to close x volume, the
    unadjusted close to the adjusted one (the same on the latest sessions).
    """
    history = history or {}
    day = sessions[-1]
    on_calendar = [d for d in sorted(bars) if d <= day]
    session_set = set(sessions)
    on_calendar = [d for d in on_calendar if d in session_set]
    has_bar = bool(on_calendar) and on_calendar[-1] == day
    raw_close = None
    if on_calendar:
        last = on_calendar[-1]
        raw = history.get(last, (math.nan,) * 3)[1]
        raw_close = float(raw) if _finite(raw) and raw > 0 else float(bars[last][0])
    recent = any(d in bars for d in sessions[-RECENT_SESSIONS:])

    values = []
    first = on_calendar[0] if on_calendar else None
    for d in sessions[-LIQ_WINDOW:]:
        if first is None or d < first:
            continue                              # not listed yet
        if d in bars:
            matched = history.get(d, (math.nan,) * 3)[0]
            if _finite(matched) and matched >= 0:
                value = float(matched)
            else:
                close, volume = bars[d]
                value = float(close) * float(volume) if _finite(close) and _finite(volume) else math.nan
            if math.isnan(value):
                continue
            values.append(value)
        else:
            values.append(0.0)                    # suspended: illiquid
    median = float(np.median(values)) if len(values) >= LIQ_MIN_PERIODS else None

    # Shares x unadjusted close of the latest price-history row at most CAP_FFILL
    # sessions old (today's row is published after the close: yesterday's is used).
    cap = None
    for d in reversed(sessions[-(CAP_FFILL + 1):]):
        row = history.get(d)
        if row is not None and _finite(row[1]) and _finite(row[2]) and row[1] > 0 and row[2] > 0:
            cap = float(row[2]) * float(row[1])
            break
    return Snapshot(symbol, hose, len(on_calendar), raw_close, recent, median, cap, has_bar)


def liquid_pool(snaps: Iterable[Snapshot], n: int = LIQUID_POOL) -> list[Snapshot]:
    """The ``n`` eligible stocks with the highest median matched value (ties: by code)."""
    eligible = [s for s in sorted(snaps, key=lambda s: s.symbol) if s.eligible]
    return sorted(eligible, key=lambda s: -s.median_value)[:n]


def cap_universe(snaps: Iterable[Snapshot], size: int, pool: int = LIQUID_POOL) -> list[str]:
    """The ``size`` largest market caps among the liquid pool, largest first."""
    liquid = [s for s in sorted(liquid_pool(snaps, pool), key=lambda s: s.symbol)
              if s.market_cap is not None and s.market_cap > 0]
    return [s.symbol for s in sorted(liquid, key=lambda s: -s.market_cap)[: max(int(size), 1)]]


class UniverseUnavailable(RuntimeError):
    """The sources did not answer for enough stocks to rank them."""


def build_universe(sessions: list[str], hose_symbols: Iterable[str], size: int, *,
                   bars_of: Callable[[str], Mapping[str, tuple[float, float]] | None],
                   history_of: Callable[[str], Mapping[str, tuple[float, float, float]] | None],
                   pool: int = LIQUID_POOL, margin: int = 40, max_failed: float = 0.2) -> dict:
    """The cap universe on ``sessions[-1]`` from the vendors.

    Every HOSE stock's price-history gives a first liquidity order; the daily
    bars (``bars_of``) are read only for the leaders of that order (``pool`` +
    ``margin``, more while the pool is not full) and for the stocks whose
    price-history did not answer. A price-history that failed is asked once
    more at the end (a passing error is not a missing stock); MAX_FAILED_IN_A_ROW
    failures in a row is an outage and stops the build. Raises
    UniverseUnavailable then, or when more than ``max_failed`` of the stocks (or
    of the pool's share counts) are missing. Returns {"universe", "pool", "caps"}.
    """
    symbols = sorted(dict.fromkeys(s.upper() for s in hose_symbols))
    if not symbols:
        raise UniverseUnavailable("no HOSE listing")
    histories: dict[str, Mapping] = {}
    failed: list[str] = []
    in_a_row = 0
    for symbol in symbols:
        try:
            histories[symbol] = history_of(symbol) or {}
            in_a_row = 0
        except Exception as exc:  # noqa: BLE001 - counted below
            failed.append(symbol)
            in_a_row += 1
            if in_a_row >= MAX_FAILED_IN_A_ROW:
                raise UniverseUnavailable(f"price-history: {in_a_row} stocks in a row failed ({exc})") from exc
    for symbol in list(failed):                   # once more: a passing error is not a missing stock
        try:
            histories[symbol] = history_of(symbol) or {}
            failed.remove(symbol)
        except Exception:  # noqa: BLE001
            continue
    if len(failed) > max_failed * len(symbols):
        raise UniverseUnavailable(f"price-history unavailable for {len(failed)}/{len(symbols)} stocks")
    window = sessions[-LIQ_WINDOW:]
    recent = set(sessions[-RECENT_SESSIONS:])

    def first_pass(symbol: str) -> float:
        rows = histories.get(symbol) or {}
        days = [d for d in rows if d <= sessions[-1]]
        if not days or not recent.intersection(days):
            return -1.0
        raw = rows[max(days)][1]
        if _finite(raw) and raw < MIN_PRICE:
            return -1.0
        first = min(days)
        vals = [float(rows[d][0]) if d in rows and _finite(rows[d][0]) else 0.0 for d in window if d >= first]
        return float(np.median(vals)) if len(vals) >= LIQ_MIN_PERIODS else -1.0

    order = sorted((s for s in symbols if s not in failed), key=lambda s: (-first_pass(s), s))
    queue = list(failed) + order
    snaps: dict[str, Snapshot] = {}
    bars_failed = 0
    take = pool + margin + len(failed)
    while True:
        for symbol in queue[:take]:
            if symbol in snaps:
                continue
            try:
                bars = bars_of(symbol)
            except Exception:  # noqa: BLE001
                bars_failed += 1
                bars = None
            snaps[symbol] = snapshot(symbol, sessions, bars or {}, histories.get(symbol), hose=True)
        liquid = liquid_pool(snaps.values(), pool)
        if len(liquid) >= pool or take >= len(queue):
            break
        take += margin
    if bars_failed > max_failed * max(len(snaps), 1):
        raise UniverseUnavailable(f"daily bars unavailable for {bars_failed}/{len(snaps)} stocks")
    missing_caps = [s.symbol for s in liquid if s.market_cap is None]
    if len(missing_caps) > max_failed * max(len(liquid), 1):
        raise UniverseUnavailable(f"no share count for {len(missing_caps)}/{len(liquid)} liquid stocks")
    universe = cap_universe(snaps.values(), size, pool)
    return {"universe": universe, "pool": [s.symbol for s in liquid],
            "caps": {s.symbol: s.market_cap for s in liquid if s.market_cap is not None}}


# ── the score ────────────────────────────────────────────────────────────────


def aligned_closes(bars: Mapping[str, float], window: list[str]) -> list[float]:
    """Closes on the sessions of ``window`` (NaN on a session without a bar)."""
    return [float(bars[d]) if d in bars and _finite(bars[d]) else math.nan for d in window]


def _average_ranks(values: list[float]) -> list[float]:
    """1-based ranks, ascending, ties averaged (pandas ``rank()``)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def rank(closes: Mapping[str, list[float]], order: list[str] | None = None) -> list[Ranked]:
    """The scored names, best first. ``closes``: each name's closes on the same
    sessions (the last SCORE_WINDOW, NaN where it did not trade). Ties in the score
    go to the lower volatility, then to the earlier name of ``order``."""
    names = [s for s in (order or list(closes)) if s in closes]
    rows = []
    for symbol in names:
        px = np.asarray(closes[symbol], dtype=float)[-SCORE_WINDOW:]
        if not len(px):
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            returns = np.diff(np.log(px))
        returns = returns[~np.isnan(returns)]
        if len(returns) < MIN_RETURNS:
            continue
        top = np.nanmax(px)
        if not math.isfinite(px[-1]) or not top > 0:
            continue
        rows.append((symbol, float(np.std(returns, ddof=1)), float(px[-1] / top)))
    if not rows:
        return []
    r_vol = _average_ranks([-v for _, v, _ in rows])
    r_hi = _average_ranks([h for _, _, h in rows])
    ranked = [Ranked(s, a + b, v, h) for (s, v, h), a, b in zip(rows, r_vol, r_hi, strict=True)]
    position = {s: i for i, s in enumerate(names)}
    return sorted(ranked, key=lambda r: (-r.score, r.vol, position[r.symbol]))


def select(ranked: list[str], held: Iterable[str], k: int, buffer: float) -> list[str]:
    """The basket: held names still in the top ``buffer`` x ``k`` stay, then the best."""
    k = max(int(k), 1)
    top = set(ranked[: int(round(k * buffer))])
    pick = [s for s in held if s in top][:k]
    for symbol in ranked:
        if len(pick) >= k:
            break
        if symbol not in pick:
            pick.append(symbol)
    return pick


# ── the calendar and the market filter ───────────────────────────────────────


def market_ok(vni_closes: list[float], ma: int) -> bool:
    """VNINDEX's last close above its ``ma``-session simple average."""
    closes = np.asarray([c for c in vni_closes if _finite(c)], dtype=float)
    ma = max(int(ma), 1)
    if len(closes) < ma:
        return False
    return bool(closes[-1] > closes[-ma:].mean())


def _week(day: str) -> str:
    year, week, _ = _date.fromisoformat(day[:10]).isocalendar()
    return f"{year}-W{week:02d}"


def _month(day: str) -> str:
    return day[:7]


def next_session_guess(day: str) -> str:
    """The next day the exchange calendar expects a session (``vn_rules.is_session_day``)."""
    d = _date.fromisoformat(day[:10]) + timedelta(days=1)
    while not vn_rules.is_session_day(d.isoformat()):
        d += timedelta(days=1)
    return d.isoformat()


def _rebuild_month(month: str, every: int) -> bool:
    return int(month[5:7]) % max(int(every), 1) == 0


def decide(book: Ledger, day: str, next_day: str | None, vni_closes: list[float], params: Params,
           ranking: Callable[[], list[Ranked]]) -> Decision:
    """Today's basket; keeps its state in ``book.meta`` (fb_*).

    ``next_day``: the session after ``day`` (None = none: counts as a week and
    month end). ``ranking`` is called only when the basket is rebuilt.
    """
    meta = book.meta
    prev = meta.get("fb_day")
    week, month = _week(day), _month(day)
    week_end = next_day is None or _week(next_day) != week
    month_end = next_day is None or _month(next_day) != month
    # A week end or month end the previous session was not known to be (a holiday guessed wrong).
    late_week = bool(prev) and _week(prev) != week and meta.get("fb_week") != _week(prev)
    late_month = bool(prev) and _month(prev) != month and meta.get("fb_month") != _month(prev)
    checked = "fb_on" not in meta or week_end or late_week
    on = market_ok(vni_closes, params.fb_market_ma) if checked else bool(meta.get("fb_on"))
    meta["fb_on"] = on
    if week_end:
        meta["fb_week"] = week
    period_end = ((month_end and _rebuild_month(month, params.fb_rebalance_months))
                  or (late_month and _rebuild_month(_month(prev), params.fb_rebalance_months)))
    if month_end:
        meta["fb_month"] = month
    meta["fb_day"] = day
    ma = params.fb_market_ma
    if not on:
        meta["fb_basket"] = None
        return Decision(False, False, [], checked, note=f"VNINDEX dưới MA{ma} - giữ tiền mặt")
    basket = meta.get("fb_basket")
    if basket is None or period_end:
        why = "cuối tháng" if basket is not None else f"VNINDEX trên MA{ma}"
        ranked = ranking()
        names = [r.symbol for r in ranked]
        basket = select(names, list(book.positions), params.fb_top_k, params.fb_buffer)
        # An empty ranking (no data) is not a basket: tried again next session.
        meta["fb_basket"] = basket if basket else None
        meta["fb_ranking"] = [[r.symbol, round(r.score, 1)]
                              for r in ranked[: int(round(params.fb_top_k * params.fb_buffer))]]
        return Decision(True, True, basket, checked, ranked, note=f"dựng lại rổ ({why})")
    return Decision(True, False, list(basket), checked)


def plan(ledger: Ledger, day: str, decision: Decision, last_close: dict[str, float], params: Params) -> list[Order]:
    """Orders at the next open that move the book to the basket (``last_close``:
    closes of the session, only for names that traded in it)."""
    orders: list[Order] = []
    keep = set(decision.basket)
    open_sells = {o.symbol for o in ledger.orders if o.side == "SELL"}
    open_buys = {o.symbol for o in ledger.orders if o.side == "BUY"}
    proceeds = 0.0
    for symbol, pos in ledger.positions.items():
        if symbol not in keep and symbol not in open_sells:
            reason = (f"VNINDEX dưới MA{params.fb_market_ma} - về tiền mặt" if not decision.market_ok
                      else "rời rổ phòng thủ")
            orders.append(Order(symbol, "SELL", pos.qty, None, day, reason))
            proceeds += pos.qty * (last_close.get(symbol) or pos.last_price)
    nav = ledger.nav()
    available = ledger.cash - ledger.reserved() + proceeds * 0.997
    weight = INVEST / max(int(params.fb_top_k), 1)
    for symbol in decision.basket:
        if symbol in ledger.positions or symbol in open_buys:
            continue
        price = last_close.get(symbol)
        if not price:
            continue
        budget = min(nav * weight, available)
        qty = vn_rules.round_lot(budget / (price * (1 + vn_rules.BUY_FEE_PCT / 100) * 1.02))  # room for the open
        if qty <= 0:
            continue
        orders.append(Order(symbol, "BUY", qty, None, day, "vào rổ phòng thủ", ref=price))
        available -= qty * price * 1.02
    ledger.orders.extend(orders)
    return orders


def run(data, params: Params, *, start: str | None = None, end: str | None = None,
        start_cash: float = 1_000_000_000.0, universe: Callable[[str], list[str]] | None = None) -> Ledger:
    """Backtest over a simulator.Dataset (its bars and VNINDEX closes).

    ``universe(day)`` gives the names ranked on ``day``; by default every stock of
    the dataset (the nightly calibration builds it from the latest cap universe).
    """
    ledger = Ledger(cash=start_cash, start_cash=start_cash)
    calendar = sorted(data.benchmark)
    values = [data.benchmark[d] for d in calendar]
    days = [d for d in data.dates if (start is None or d >= start) and (end is None or d <= end)]
    closes = {s: {d: b.close for d, b in bars.items()} for s, bars in data.bars.items()}
    for day in days:
        active = {o.symbol for o in ledger.orders} | set(ledger.positions)
        bars = {s: data.bars[s][day] for s in active if day in data.bars.get(s, {})}
        ledger.process_session(bars, rules=RULES)
        ledger.end_of_session(day, data.benchmark.get(day))
        i = bisect.bisect_right(calendar, day)
        next_day = calendar[i] if i < len(calendar) else None

        def ranking(day=day, i=i):
            window = calendar[max(0, i - SCORE_WINDOW):i]
            names = universe(day) if universe is not None else list(data.symbols or data.bars)
            names = [s for s in names if day in data.bars.get(s, {})]
            return rank({s: aligned_closes(closes[s], window) for s in names}, names)

        decision = decide(ledger, day, next_day, values[:i][-max(params.fb_market_ma, 1):], params, ranking)
        last = {s: data.bars[s][day].close for s in set(decision.basket) | set(ledger.positions)
                if day in data.bars.get(s, {})}
        plan(ledger, day, decision, last, params)
    return ledger
