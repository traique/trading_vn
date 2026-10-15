"""Backtester that drives the repo's REAL ledger (tradingagents.trading.ledger).

Each session D of [start, end]:

1. ``ledger.process_session(bars_of_D, rules=strategy.rules)`` - fills the
   orders planned at D-1's close and applies exits (T+2, lots, ticks, fees,
   sell tax, slippage, limit-locked sessions: all the repo's code).
2. ``ledger.end_of_session(D, VNINDEX close)``.
3. ``strategy(ctx)`` at D's close, where ``ctx`` only exposes data up to D
   (every panel is sliced ``iloc[:i+1]``). It returns orders for D+1:
   - ``None``: nothing to do;
   - ``dict[symbol, weight]``: target basket - held symbols missing from it
     are sold at the next open, new symbols are bought at the next open with
     ``weight * NAV`` (held ones are NOT resized: the ledger enters once and
     exits fully);
   - ``list[ledger.Order]`` (use ``ctx.buy`` / ``ctx.sell``), for limit
     orders, stops, targets.
4. Optional liquidity cap on new BUYs: qty <= ``max_adv_pct`` x the 20-session
   average traded volume (value-based, so it is correct across adjustments).
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research import metrics as M
from research.config import LOCK_AFTER
from research.data import Panel, load_panel
from research.universe import universe as _universe
from tradingagents.market import vn_rules
from tradingagents.trading.ledger import Bar, Ledger, Order, _ExitRules

# Hold until the strategy sells: no T1/T2 targets, no time stop (the momentum book's rules).
HOLD_RULES = _ExitRules(max_hold_sessions=10**9, t1_sell_fraction=0.0, use_t2=False, trail_pct=0.0)


class TestPeriodLocked(RuntimeError):
    __test__ = False   # not a pytest class


@contextlib.contextmanager
def slippage(mult: float):
    """Scale ``vn_rules.SLIPPAGE_PCT`` (read at call time by the ledger) for one run. Not thread-safe."""
    base = vn_rules.SLIPPAGE_PCT
    vn_rules.SLIPPAGE_PCT = base * mult
    try:
        yield
    finally:
        vn_rules.SLIPPAGE_PCT = base


class _Arrays:
    """numpy views of a panel for fast Bar construction."""

    def __init__(self, panel: Panel):
        self.cols = {s: j for j, s in enumerate(panel.close.columns)}
        self.o = panel.open.to_numpy()
        self.h = panel.high.to_numpy()
        self.l = panel.low.to_numpy()
        self.c = panel.close.to_numpy()
        self.v = panel.volume.to_numpy()
        self.ex = panel.exchange.to_numpy()
        has = ~np.isnan(self.c)
        rows = np.where(has, np.arange(len(panel.dates))[:, None], np.nan)
        prev_row = pd.DataFrame(rows).ffill().shift(1).to_numpy()
        self.prev_row = prev_row
        self.dates = [d.strftime("%Y-%m-%d") for d in panel.dates]
        val = panel.value.where(panel.close.notna(), 0.0)
        self.adv20_value = val.rolling(20, min_periods=1).mean().to_numpy()

    def bar(self, sym: str, i: int) -> Bar | None:
        j = self.cols.get(sym)
        if j is None or math.isnan(self.c[i, j]):
            return None
        pr = self.prev_row[i, j]
        prev_close = prev_date = None
        if not math.isnan(pr):
            prev_close = float(self.c[int(pr), j])
            prev_date = self.dates[int(pr)]
        ex = self.ex[i, j]
        vol = self.v[i, j]
        return Bar(self.dates[i], float(self.o[i, j]), float(self.h[i, j]), float(self.l[i, j]), float(self.c[i, j]),
                   prev_close, exchange=ex if isinstance(ex, str) else None,
                   volume=None if math.isnan(vol) else float(vol), prev_date=prev_date)


class Context:
    """What a strategy sees at the close of session ``date`` (no later data)."""

    def __init__(self, panel: Panel, arrays: _Arrays, i: int, ledger: Ledger, universe_kind: str, state: dict):
        self._panel, self._arrays, self.i = panel, arrays, i
        self.date = panel.dates[i]
        self.day = arrays.dates[i]
        self.ledger = ledger
        self.universe_kind = universe_kind
        self.state = state                       # free per-run storage for the strategy

    # -- point-in-time data (sliced to sessions <= date) --
    def _cut(self, frame):
        return frame.iloc[: self.i + 1]

    @property
    def open(self) -> pd.DataFrame: return self._cut(self._panel.open)
    @property
    def high(self) -> pd.DataFrame: return self._cut(self._panel.high)
    @property
    def low(self) -> pd.DataFrame: return self._cut(self._panel.low)
    @property
    def close(self) -> pd.DataFrame: return self._cut(self._panel.close)
    @property
    def volume(self) -> pd.DataFrame: return self._cut(self._panel.volume)
    @property
    def value(self) -> pd.DataFrame: return self._cut(self._panel.value)
    @property
    def raw_close(self) -> pd.DataFrame: return self._cut(self._panel.raw_close)
    @property
    def exchange(self) -> pd.DataFrame: return self._cut(self._panel.exchange)
    @property
    def vnindex(self) -> pd.Series: return self._cut(self._panel.vnindex)
    @property
    def vn30(self) -> pd.Series: return self._cut(self._panel.vn30)
    @property
    def vnindex_bars(self) -> pd.DataFrame:
        return self._panel.vnindex_bars[self._panel.vnindex_bars.index <= self.date]

    def history(self, field: str, symbols=None, n: int | None = None) -> pd.DataFrame:
        """Last ``n`` sessions (<= date) of a field, optionally for some symbols."""
        frame = getattr(self, field)
        if symbols is not None:
            frame = frame.reindex(columns=list(symbols))
        return frame if n is None else frame.iloc[-n:]

    def closes_by_bar(self, symbols, n: int) -> dict[str, list[float]]:
        """Each symbol's last ``n`` closes over ITS OWN bars (suspended sessions skipped),
        only for symbols that traded today - the series the repo's momentum code ranks."""
        cut = self._panel.close.iloc[max(0, self.i + 1 - (n + 400)): self.i + 1]
        out = {}
        for s in symbols:
            if s not in cut.columns or math.isnan(cut[s].iat[-1]):
                continue
            out[s] = cut[s].dropna().iloc[-n:].tolist()
        return out

    def universe(self, kind: str | None = None) -> list[str]:
        """The universe for the next session (built from data up to today)."""
        return _universe(self.date + pd.Timedelta(days=1), kind or self.universe_kind, self._panel)

    def has_bar(self, symbol: str) -> bool:
        return self._arrays.bar(symbol, self.i) is not None

    def last_close(self, symbol: str) -> float | None:
        j = self._arrays.cols.get(symbol)
        if j is None:
            return None
        v = self._arrays.c[self.i, j]
        return None if math.isnan(v) else float(v)

    def adv20_value(self, symbol: str) -> float:
        j = self._arrays.cols.get(symbol)
        return 0.0 if j is None else float(self._arrays.adv20_value[self.i, j])

    # -- book --
    @property
    def nav(self) -> float: return self.ledger.nav()
    @property
    def cash(self) -> float: return self.ledger.cash
    @property
    def positions(self) -> dict: return self.ledger.positions

    # -- order helpers --
    def buy(self, symbol: str, *, qty: int | None = None, value: float | None = None, weight: float | None = None,
            limit: float | None = None, stop: float | None = None, stop_pct: float = 0.0,
            target1: float | None = None, target2: float | None = None, reason: str = "") -> Order | None:
        """A BUY for the next session: at the open (``limit`` None) or a one-session limit.
        Size by ``qty`` shares, ``value`` VND or ``weight`` of NAV (lot-rounded, 2% gap room at the open)."""
        price = limit or self.last_close(symbol)
        if not price:
            return None
        if qty is None:
            budget = value if value is not None else (weight or 0.0) * self.ledger.nav()
            room = 1.0 if limit else 1.02
            qty = vn_rules.round_lot(budget / (price * (1 + vn_rules.BUY_FEE_PCT / 100) * room))
        if qty <= 0:
            return None
        return Order(symbol, "BUY", int(qty), limit, self.day, reason or "harness buy", stop, target1, target2,
                     ref=self.last_close(symbol), stop_pct=stop_pct)

    def sell(self, symbol: str, reason: str = "") -> Order | None:
        """Sell the whole (sellable) position at the next open."""
        pos = self.ledger.positions.get(symbol)
        if pos is None:
            return None
        return Order(symbol, "SELL", pos.qty, None, self.day, reason or "harness sell")

    def orders_for_weights(self, weights: dict[str, float], reason: str = "rebalance") -> list[Order]:
        """Orders that move the book to the basket ``weights`` (momentum.plan's logic:
        sell what left the basket, buy new names at weight x NAV, keep held ones as they are)."""
        led = self.ledger
        targets = {s: w for s, w in weights.items() if w and w > 0}
        open_sells = {o.symbol for o in led.orders if o.side == "SELL"}
        open_buys = {o.symbol for o in led.orders if o.side == "BUY"}
        orders, proceeds = [], 0.0
        for sym, pos in led.positions.items():
            if sym not in targets and sym not in open_sells:
                orders.append(Order(sym, "SELL", pos.qty, None, self.day, reason))
                proceeds += pos.qty * (self.last_close(sym) or pos.last_price)
        nav = led.nav()
        available = led.cash - led.reserved() + proceeds * 0.997
        for sym, w in sorted(targets.items(), key=lambda kv: -kv[1]):
            if sym in led.positions or sym in open_buys:
                continue
            price = self.last_close(sym)
            if not price:
                continue
            budget = min(nav * w, available)
            qty = vn_rules.round_lot(budget / (price * (1 + vn_rules.BUY_FEE_PCT / 100) * 1.02))
            if qty <= 0:
                continue
            orders.append(Order(sym, "BUY", qty, None, self.day, reason, ref=price))
            available -= qty * price * 1.02
        return orders


@dataclass
class Result:
    name: str
    start: str
    end: str
    universe_kind: str
    ledger: Ledger
    daily: pd.DataFrame              # date-indexed: nav, cash, invested, exposure, bench
    fills: pd.DataFrame
    metrics: dict
    benchmark: dict                  # VNINDEX buy & hold over the same sessions
    yearly: pd.DataFrame
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        m, b = self.metrics, self.benchmark
        return (f"{self.name} [{self.universe_kind}] {self.start}..{self.end}: "
                f"ret {m['total_return_pct']:+.1f}% CAGR {m['cagr_pct']:+.1f}% MDD {m['max_drawdown_pct']:.1f}% "
                f"Sharpe {m['sharpe']:.2f} | VNINDEX {b['total_return_pct']:+.1f}% CAGR {b['cagr_pct']:+.1f}%")


def _apply_liquidity_cap(ctx: Context, orders: list[Order], max_adv_pct: float) -> list[Order]:
    out = []
    for o in orders:
        if o.side == "BUY":
            price = o.limit or o.ref or ctx.last_close(o.symbol)
            if not price:
                continue
            cap = vn_rules.round_lot(max_adv_pct * ctx.adv20_value(o.symbol) / price)
            if cap <= 0:
                continue
            o.qty = min(o.qty, cap)
        out.append(o)
    return out


def run(strategy, start: str, end: str, *, capital: float = 1e9, universe_kind: str = "liq_top30",
        slippage_mult: float = 1.0, max_adv_pct: float | None = None, unlock_test: bool = False,
        rules: _ExitRules | None = None, panel: Panel | None = None, name: str | None = None) -> Result:
    """Backtest ``strategy`` over sessions [start, end] with the repo's ledger.

    ``strategy`` is an object with ``__call__(ctx)`` or ``on_session(ctx)``
    (optional ``rules`` attribute = ledger exit rules, ``name``,
    ``on_start(ctx)``, ``expire_orders`` = drop orders older than the previous
    session before each session, as the bot does) or a plain function ``f(ctx)``.
    """
    if pd.Timestamp(end) > pd.Timestamp(LOCK_AFTER) and not unlock_test:
        raise TestPeriodLocked(f"end {end} is in the locked TEST period (after {LOCK_AFTER}); "
                               "only the evaluator may pass unlock_test=True")
    panel = panel or load_panel(unlock_test=unlock_test)
    if not unlock_test and panel.dates[-1] > pd.Timestamp(LOCK_AFTER):
        raise TestPeriodLocked("panel holds TEST sessions; load it with load_panel() (locked)")
    arrays = _Arrays(panel)
    idx = np.where((panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end)))[0]
    if len(idx) == 0:
        raise ValueError(f"no sessions between {start} and {end}")
    call: Callable = getattr(strategy, "on_session", None) or strategy
    rules = rules or getattr(strategy, "rules", None) or HOLD_RULES
    sname = name or getattr(strategy, "name", None) or getattr(strategy, "__name__", type(strategy).__name__)
    ledger = Ledger(cash=capital, start_cash=capital)
    state: dict = {}
    rows = []
    warnings: list[str] = []
    expire = bool(getattr(strategy, "expire_orders", False))
    with slippage(slippage_mult):
        for k, i in enumerate(idx):
            day = arrays.dates[i]
            if expire and i > 0:
                # The bot's rule (vnbot.trader._expire): an order created before the previous
                # session was meant for a session that has passed; it lapses instead of filling now.
                prev = arrays.dates[i - 1]
                ledger.orders = [o for o in ledger.orders if o.created >= prev]
            active = {o.symbol for o in ledger.orders} | set(ledger.positions)
            bars = {}
            for s in active:
                b = arrays.bar(s, i)
                if b is not None:
                    bars[s] = b
            ledger.process_session(bars, rules=rules)
            bench = float(panel.vnindex.iloc[i])
            ledger.end_of_session(day, bench)
            ctx = Context(panel, arrays, i, ledger, universe_kind, state)
            if k == 0 and hasattr(strategy, "on_start"):
                strategy.on_start(ctx)
            before = len(ledger.orders)
            out = call(ctx)
            new = list(ledger.orders[before:])          # orders the strategy placed itself
            del ledger.orders[before:]
            if isinstance(out, dict):
                new += ctx.orders_for_weights(out)
            elif out:
                new += [o for o in out if o is not None]
            for o in new:
                o.created = day
            if max_adv_pct is not None:
                new = _apply_liquidity_cap(ctx, new, max_adv_pct)
            ledger.orders.extend(new)
            nav = ledger.nav()
            rows.append((panel.dates[i], nav, ledger.cash, nav - ledger.cash, bench))
    daily = pd.DataFrame(rows, columns=["date", "nav", "cash", "invested", "bench"]).set_index("date")
    daily["exposure"] = (daily["invested"] / daily["nav"]).clip(lower=0)
    last_i = idx[-1]
    for s, pos in ledger.positions.items():
        j = arrays.cols.get(s)
        if j is not None and math.isnan(arrays.c[last_i, j]):
            warnings.append(f"{s}: held at the end without a bar on the last session (marked at {pos.last_date})")
    fills = pd.DataFrame([f.__dict__ for f in ledger.fills])
    met = M.compute(daily["nav"], capital, daily["exposure"], fills)
    bench_nav = daily["bench"] / daily["bench"].iloc[0] * capital
    bmet = M.compute(bench_nav, capital, pd.Series(1.0, index=daily.index), None)
    yearly = M.yearly(daily["nav"], capital, daily["exposure"], daily["bench"])
    label = getattr(strategy, "universe_label", None) or universe_kind
    return Result(sname, str(panel.dates[idx[0]].date()), str(panel.dates[last_i].date()), label, ledger,
                  daily, fills, met, bmet, yearly, warnings)


def run_index(start: str, end: str, *, capital: float = 1e9, unlock_test: bool = False, panel: Panel | None = None,
              name: str = "vnindex_buyhold") -> Result:
    """VNINDEX buy & hold over [start, end]: the index itself (a price index, no
    dividends, no costs), NAV = capital x index / its first close in the range."""
    if pd.Timestamp(end) > pd.Timestamp(LOCK_AFTER) and not unlock_test:
        raise TestPeriodLocked(f"end {end} is in the locked TEST period (after {LOCK_AFTER}); "
                               "only the evaluator may pass unlock_test=True")
    panel = panel or load_panel(unlock_test=unlock_test)
    s = panel.vnindex[(panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end))].dropna()
    if s.empty:
        raise ValueError(f"no sessions between {start} and {end}")
    nav = s / s.iloc[0] * capital
    exposure = pd.Series(1.0, index=nav.index)
    daily = pd.DataFrame({"nav": nav, "cash": 0.0, "invested": nav, "bench": s, "exposure": exposure})
    daily.index.name = "date"
    met = M.compute(nav, capital, exposure, None)
    yearly = M.yearly(nav, capital, exposure, s)
    return Result(name, str(s.index[0].date()), str(s.index[-1].date()), "-", Ledger(cash=capital, start_cash=capital),
                  daily, pd.DataFrame(), met, dict(met), yearly)
