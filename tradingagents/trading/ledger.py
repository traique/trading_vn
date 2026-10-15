"""A trading book under Vietnamese rules, and the order/exit rules of the bot.

One set of rules serves the paper account and the backtest, so a backtest
result is what the paper account would have done:

- Decisions are taken on a session's close; orders go to the NEXT session.
- BUY: a limit order at the top of the engine's entry zone, good for one
  session. It fills only when the session trades THROUGH the limit (low below
  it): a session whose low only equals the limit fills nothing, as the queue
  at that price may never reach the order. The fill is the limit, or the open
  plus slippage when the open is lower (never above the limit). An order at
  the open (``limit`` None) fills at the open plus slippage. Size is the
  engine's % of NAV, rounded down to lots of 100 and to the cash on hand. A
  one-price session that closes up (locked at the ceiling) fills no buy.
- Slippage: every simulated fill at a market price (the open, the close, a
  stop) moves ``vn_rules.SLIPPAGE_PCT`` against the trader and onto the tick
  grid in that direction (``vn_rules.slipped``), but never outside the
  session's [low, high]. A limit fill (entry, T1, T2) is never worse than its
  limit.
- Settlement T+2 (``vn_rules.SETTLEMENT_SESSIONS``): shares bought on T
  arrive in the afternoon of T+2. On that bar they can only leave in the
  afternoon, modelled at the close: a pending SELL order fills at the close;
  the stop triggers only if the close is at or under it and fills at the
  close; T1/T2 only if the close reaches them, filled at the target. From the
  third session after T the full open-to-close rules below apply. Each buy is
  a lot with its own clock (``Position.unsettled``), so shares added to a
  holding are not sellable before they settle.
- Exits, once sellable, in this order each session: a SELL order (the engine
  turned to SELL) at the open; the stop (at the open if it gapped below);
  ``t1_sell_fraction`` at T1 (then the stop moves to break-even, and trails
  ``trail_pct`` under the highest close if set) and the rest at T2 (unless
  ``use_t2`` is off); a time stop at the close after ``max_hold_sessions``.
  A target fills when the session trades through it (high above it). When a
  session touches both the stop and a target, the stop is assumed first. A
  one-price session that closes down (locked at the floor) fills no sell.
- Cash from a sale at the close cannot pay for that session's buys, which
  fill earlier in the day; cash from a sale at the open can (brokers allow it).
- A bar with a missing, non-finite or non-positive price, or an impossible
  range, is a session without trading: no fill, no stop, no settlement step.
- Fees and the 0.1% sell tax from ``vn_rules`` are charged on every fill;
  the buy fee is part of the cost basis.
- A saved book that cannot be read is never replaced by an empty one behind
  the owner's back: ``Ledger.load`` moves it aside and raises (see there).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import date as _date, datetime, timedelta
from pathlib import Path

from tradingagents.market import vn_rules

logger = logging.getLogger(__name__)

# One source of truth: vn_rules. Kept here for the modules that import it from the ledger.
SETTLEMENT_SESSIONS = vn_rules.SETTLEMENT_SESSIONS


class LedgerLoadError(RuntimeError):
    """A saved book exists but cannot be used; it was not overwritten."""


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None = None
    exchange: str | None = None      # sets the tick grid of slipped fills (HOSE grid when unknown)
    volume: float | None = None      # when given, a session with no volume is a session without trading
    prev_date: str | None = None     # the session ``prev_close`` belongs to (detects a price-basis change)

    @property
    def tradable(self) -> bool:
        """Whether the session traded at believable prices (forward-filled NaN rows do not)."""
        prices = (self.open, self.high, self.low, self.close)
        if not all(finite(p) and float(p) > 0 for p in prices):
            return False
        if self.low > self.high:
            return False
        tol = 1e-9 * self.high
        if not (self.low - tol <= self.open <= self.high + tol and self.low - tol <= self.close <= self.high + tol):
            return False
        return self.volume is None or (finite(self.volume) and float(self.volume) > 0)

    @property
    def locked_up(self) -> bool:
        return self.high == self.low and self.prev_close is not None and self.close > self.prev_close

    @property
    def locked_down(self) -> bool:
        return self.high == self.low and self.prev_close is not None and self.close < self.prev_close


@dataclass
class Signal:
    """The part of an engine decision the bot trades on."""

    symbol: str
    action: str
    confidence: float
    price: float
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    position_size_pct: float | None = None

    @classmethod
    def from_result(cls, r) -> Signal:
        return cls(symbol=r.symbol, action=r.action, confidence=r.confidence, price=r.price,
                   entry_low=r.entry_low, entry_high=r.entry_high, stop=r.stop_price,
                   target1=r.target_price, target2=r.target2, position_size_pct=r.position_size_pct)


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float           # cost per share, buy fee included
    buy_date: str              # the first buy
    stop: float = 0.0
    target1: float | None = None
    target2: float | None = None
    sessions: int = 0          # sessions traded since the first buy session
    t1_done: bool = False
    last_price: float = 0.0
    peak: float = 0.0          # highest close since the buy
    # Bought lots not yet settled for the next session's open: {"date", "qty", "sessions"}.
    # None (an old save or a hand-made Position) means one lot of ``qty`` aged ``sessions``.
    unsettled: list[dict] | None = None
    last_date: str = ""        # the session ``last_price`` is the close of ("" when unknown)

    def __post_init__(self) -> None:
        if self.unsettled is None:
            self.unsettled = ([{"date": self.buy_date, "qty": int(self.qty), "sessions": int(self.sessions)}]
                              if self.sessions < SETTLEMENT_SESSIONS and self.qty > 0 else [])
        else:
            self.unsettled = [{"date": str(lot.get("date") or self.buy_date), "qty": int(lot["qty"]),
                               "sessions": int(lot.get("sessions") or 0)}
                              for lot in self.unsettled if int(lot.get("qty") or 0) > 0]

    @property
    def settled_qty(self) -> int:
        """Shares that can be sold from the next session's open."""
        pending = sum(lot["qty"] for lot in self.unsettled if lot["sessions"] < SETTLEMENT_SESSIONS)
        return max(0, self.qty - pending)

    @property
    def sellable(self) -> bool:
        return self.settled_qty > 0

    # Within a session, after ``_advance``: shares sellable at its open, and by its close.
    def _sellable_at(self, *, close: bool) -> int:
        need = SETTLEMENT_SESSIONS if close else SETTLEMENT_SESSIONS + 1
        return max(0, self.qty - sum(lot["qty"] for lot in self.unsettled if lot["sessions"] < need))

    def _advance(self, day: str) -> None:
        self.sessions += 1
        for lot in self.unsettled:
            if day > lot["date"]:
                lot["sessions"] += 1

    def _prune(self) -> None:
        self.unsettled = [lot for lot in self.unsettled if lot["sessions"] < SETTLEMENT_SESSIONS]

    def _trim_unsettled(self) -> None:
        """Keep the unsettled lots within ``qty`` (after a sale recorded past the rules)."""
        excess = sum(lot["qty"] for lot in self.unsettled) - self.qty
        for lot in self.unsettled:           # oldest first: the closest to settling
            if excess <= 0:
                break
            cut = min(excess, lot["qty"])
            lot["qty"] -= cut
            excess -= cut
        self.unsettled = [lot for lot in self.unsettled if lot["qty"] > 0]


@dataclass
class Order:
    symbol: str
    side: str                  # BUY | SELL
    qty: int
    limit: float | None        # None = at the open
    created: str               # the session whose close produced it
    reason: str = ""
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    ref: float | None = None   # price the size was computed at (for an order at the open)
    stop_pct: float = 0.0      # stop this % under the fill price, when no absolute stop is given


@dataclass
class Fill:
    date: str
    symbol: str
    side: str
    qty: int
    price: float
    cost: float                # fees + tax
    reason: str
    pnl: float | None = None   # realised, sells only


@dataclass(frozen=True)
class _ExitRules:
    max_hold_sessions: int = 30
    t1_sell_fraction: float = 0.5
    use_t2: bool = True
    trail_pct: float = 0.0

    @classmethod
    def of(cls, params, max_hold_sessions: int) -> _ExitRules:
        if params is None:
            return cls(max_hold_sessions=max_hold_sessions)
        return cls(params.max_hold_sessions, params.t1_sell_fraction, bool(params.use_t2), params.trail_pct)


def _fee(value: float, side: str) -> float:
    if side == "BUY":
        return value * vn_rules.BUY_FEE_PCT / 100
    return value * (vn_rules.SELL_FEE_PCT + vn_rules.SELL_TAX_PCT) / 100


def _fill_price(raw: float, side: str, bar: Bar) -> float:
    """A market fill at ``raw``: slipped against the trader, kept inside the session's range."""
    price = vn_rules.slipped(raw, side, bar.exchange)
    return min(price, bar.high) if side == "BUY" else max(price, bar.low)


def _target_fill(target: float, bar: Bar, afternoon: bool) -> float | None:
    """Where a resting sell limit at ``target`` fills this session, or None."""
    if afternoon:
        # Shares arrived after lunch: judge the target on the close, fill no better than it.
        return target if bar.close >= target else None
    if bar.open > target:
        return max(_fill_price(bar.open, "SELL", bar), target)
    if bar.high > target:
        return target
    return None


# A previous close this much below the close the book marked that session is a
# corporate action (a cash dividend of ~1% or more, bonus shares, rights), not noise.
BASIS_MIN_RATIO = 1.01
BASIS_MAX_RATIO = 5.0


def _basis_ratio(marked: float | None, marked_date: str | None, bar: Bar | None) -> float | None:
    """old basis / new basis when ``bar`` re-prices the session the book last marked at
    ``marked``; None when nothing changed or the two cannot be compared."""
    if bar is None or not bar.prev_date or not marked_date or bar.prev_date != marked_date:
        return None
    if not (_positive(marked) and _positive(bar.prev_close)):
        return None
    ratio = float(marked) / float(bar.prev_close)
    return ratio if BASIS_MIN_RATIO <= ratio <= BASIS_MAX_RATIO else None


def _rebased(level: float | None, ratio: float, exchange: str | None) -> float | None:
    if not _positive(level):
        return level
    return vn_rules.round_to_tick(float(level) / ratio, exchange)


def _sessions_between(first: str, last: str) -> int:
    """Weekdays after ``first`` up to and including ``last`` (holidays not known: an upper bound)."""
    a, b = _date.fromisoformat(first[:10]), _date.fromisoformat(last[:10])
    if b <= a:
        return 0
    if (b - a).days > 14:
        return (b - a).days               # far past settlement; the exact count does not matter
    return sum(1 for i in range(1, (b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5)


def _vnd(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


@dataclass
class Ledger:
    cash: float
    start_cash: float
    start_date: str = ""
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)   # {date, nav, benchmark}
    meta: dict = field(default_factory=dict)            # strategy state (e.g. the momentum basket)

    # ── valuation ──
    def nav(self) -> float:
        return self.cash + sum(p.qty * (p.last_price or p.avg_price) for p in self.positions.values())

    def reserved(self) -> float:
        slip = 1 + vn_rules.SLIPPAGE_PCT / 100
        return sum(o.qty * (o.limit or (o.ref or 0) * slip) * (1 + vn_rules.BUY_FEE_PCT / 100)
                   for o in self.orders if o.side == "BUY")

    # ── one session ──
    def process_session(self, bars: dict[str, Bar], *, params=None, rules: _ExitRules | None = None) -> list[Fill]:
        """Apply one session to the whole book: every exit first (cash from sales at
        the open pays for the buys, as brokers allow), then the buy orders."""
        rules = rules or _ExitRules.of(params, 30)
        self._rebase(bars)
        fills: list[Fill] = []
        close_cash = 0.0                       # proceeds of sales at the close: too late for today's buys
        for symbol in list(self.positions):
            bar = bars.get(symbol)
            pos = self.positions[symbol]
            if bar is not None and bar.tradable and bar.date > pos.buy_date:
                pos._advance(bar.date)
                out, late_cash = self._exits(pos, bar, rules)
                fills += out
                close_cash += late_cash
        for order in list(self.orders):
            bar = bars.get(order.symbol)
            if bar is None or order.created >= bar.date:
                continue
            self.orders.remove(order)
            if order.side == "BUY" and order.symbol not in self.positions and bar.tradable:
                fill = self._buy(order, bar, self.cash - close_cash)
                if fill:
                    fills.append(fill)
        for symbol, pos in self.positions.items():
            bar = bars.get(symbol)
            if bar is not None and bar.tradable:
                self._mark(pos, bar, rules)
        return fills

    # ── corporate actions (dividends, bonus shares, rights) ──
    def _rebase(self, bars: dict[str, Bar]) -> None:
        """Put the book on the price basis of this session's bars.

        The sources adjust their whole history for dividends and bonus issues as
        of today, so on an ex-date the previous close (``bar.prev_close``) drops
        below the close the book marked that same session. Left alone, the old
        stop would sell the position at a fake 15-20% "loss". The holder got the
        value back as shares (or cash): the position gets ``ratio`` times the
        shares (rounded down, no share is made up) at the same total cost, and
        every level (stop, targets, peak) is divided by ``ratio``. Open orders
        planned on the old basis are rescaled the same way.
        """
        for symbol, pos in self.positions.items():
            bar = bars.get(symbol)
            ratio = _basis_ratio(pos.last_price, pos.last_date, bar)
            if ratio is None:
                continue
            old_qty, cost = pos.qty, pos.avg_price * pos.qty
            pos.qty = int(pos.qty * ratio + 1e-6)
            for lot in pos.unsettled:
                lot["qty"] = int(lot["qty"] * ratio + 1e-6)
            pos.unsettled = [lot for lot in pos.unsettled if lot["qty"] > 0]
            pos.avg_price = cost / pos.qty if pos.qty else pos.avg_price / ratio
            pos.stop = _rebased(pos.stop, ratio, bar.exchange)
            pos.target1 = _rebased(pos.target1, ratio, bar.exchange)
            pos.target2 = _rebased(pos.target2, ratio, bar.exchange)
            pos.peak = pos.peak / ratio if pos.peak else pos.peak
            pos.last_price = float(bar.prev_close)
            self._note_adjustment(bar.date, symbol, ratio, old_qty, pos.qty)
            for order in self.orders:
                if order.symbol == symbol and order.side == "SELL":
                    order.qty = min(int(order.qty * ratio + 1e-6), pos.qty)
        for order in self.orders:
            bar = bars.get(order.symbol)
            if order.side != "BUY" or order.symbol in self.positions or bar is None:
                continue
            ratio = _basis_ratio(order.ref, order.created, bar)
            if ratio is None:
                continue
            order.limit = _rebased(order.limit, ratio, bar.exchange)
            order.stop = _rebased(order.stop, ratio, bar.exchange)
            order.target1 = _rebased(order.target1, ratio, bar.exchange)
            order.target2 = _rebased(order.target2, ratio, bar.exchange)
            order.ref = float(bar.prev_close)
            order.qty = vn_rules.round_lot(order.qty * ratio)

    def _note_adjustment(self, day: str, symbol: str, ratio: float, before: int, after: int) -> None:
        logger.warning("%s %s: price basis changed x%.4f (dividend/bonus/rights); %d -> %d shares",
                       day, symbol, ratio, before, after)
        notes = self.meta.setdefault("adjustments", [])
        notes.append({"date": day, "symbol": symbol, "ratio": round(ratio, 6), "before": before, "after": after})
        del notes[:-50]

    def _mark(self, pos: Position, bar: Bar, rules: _ExitRules) -> None:
        pos.last_price = bar.close
        pos.last_date = bar.date
        pos.peak = max(pos.peak, bar.close)
        if pos.t1_done and rules.trail_pct:
            pos.stop = max(pos.stop, vn_rules.round_to_tick(pos.peak * (1 - rules.trail_pct / 100)))
        if bar.date > pos.buy_date:
            pos._prune()

    # ── one session for one symbol ──
    def process_bar(self, symbol: str, bar: Bar, *, max_hold_sessions: int = 30, params=None) -> list[Fill]:
        """Apply one session to this symbol's open orders and position. Returns the fills.

        ``params`` (quant.params.Params) sets the exit rules; ``max_hold_sessions``
        alone is kept for callers that pass only that."""
        return self.process_session({symbol: bar}, rules=_ExitRules.of(params, max_hold_sessions))

    def end_of_session(self, date: str, benchmark: float | None = None) -> None:
        """Drop the orders that had their session (suspended symbols included) and record NAV."""
        self.orders = [o for o in self.orders if o.created >= date]
        if not self.start_date:
            self.start_date = date
        if benchmark is not None and not finite(benchmark):
            benchmark = None
        self.history = [h for h in self.history if h["date"] != date]
        self.history.append({"date": date, "nav": round(self.nav()), "benchmark": benchmark})

    def _buy(self, order: Order, bar: Bar, cash: float) -> Fill | None:
        if bar.locked_up:
            return None
        if order.limit is None:
            price = _fill_price(bar.open, "BUY", bar)
        elif bar.low < order.limit:
            # Traded through the limit: at the open plus slippage if that is lower, else at the limit.
            price = min(_fill_price(bar.open, "BUY", bar), order.limit) if bar.open < order.limit else order.limit
        else:
            return None
        affordable = vn_rules.round_lot(max(cash, 0.0) / (price * (1 + vn_rules.BUY_FEE_PCT / 100)))
        qty = min(order.qty, affordable)
        if qty <= 0:
            return None
        value = qty * price
        fee = _fee(value, "BUY")
        self.cash -= value + fee
        stop = order.stop or (vn_rules.round_to_tick(price * (1 - order.stop_pct / 100)) if order.stop_pct else 0.0)
        self._add_shares(order.symbol, qty, value + fee, bar.date, stop=stop, target1=order.target1,
                         target2=order.target2, last_price=bar.close)
        fill = Fill(bar.date, order.symbol, "BUY", qty, price, round(fee), order.reason or "mua theo tín hiệu")
        self.fills.append(fill)
        return fill

    def _add_shares(self, symbol: str, qty: int, cost: float, day: str, *, stop: float | None,
                    target1: float | None, target2: float | None, last_price: float) -> Position:
        """Book ``qty`` new shares (``cost`` = value + fee) as an unsettled lot."""
        lot = {"date": day, "qty": qty, "sessions": 0}
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol, qty=qty, avg_price=cost / qty, buy_date=day, stop=float(stop or 0.0),
                           target1=target1, target2=target2, last_price=last_price, peak=last_price,
                           unsettled=[lot], last_date=day)
            self.positions[symbol] = pos
            return pos
        # Adding to a holding: the average cost moves, the new lot settles on its own clock,
        # the levels already set stay (missing ones are filled in).
        total = pos.qty + qty
        pos.avg_price = (pos.avg_price * pos.qty + cost) / total
        pos.qty = total
        pos.unsettled.append(lot)
        pos.stop = pos.stop or float(stop or 0.0)
        pos.target1 = pos.target1 if pos.target1 is not None else target1
        pos.target2 = pos.target2 if pos.target2 is not None else target2
        return pos

    def _sell(self, pos: Position, qty: int, price: float, date: str, reason: str) -> Fill:
        qty = min(qty, pos.qty)
        value = qty * price
        cost = _fee(value, "SELL")
        self.cash += value - cost
        pnl = value - cost - pos.avg_price * qty
        pos.qty -= qty
        if pos.qty <= 0:
            self.positions.pop(pos.symbol, None)
        else:
            pos._trim_unsettled()
        fill = Fill(date, pos.symbol, "SELL", qty, price, round(cost), reason, round(pnl))
        self.fills.append(fill)
        return fill

    def _exits(self, pos: Position, bar: Bar, rules: _ExitRules) -> tuple[list[Fill], float]:
        """The exits of one session (after ``pos._advance``). Returns (fills, cash from sales at the close)."""
        if bar.locked_down:
            return [], 0.0
        open_qty = pos._sellable_at(close=False)
        late_qty = pos._sellable_at(close=True) - open_qty      # lots arriving this afternoon (T+2)
        if open_qty <= 0 and late_qty <= 0:
            return [], 0.0
        sell_orders = [o for o in self.orders if o.symbol == pos.symbol and o.side == "SELL" and o.created < bar.date]
        for order in sell_orders:
            self.orders.remove(order)
        order = sell_orders[0] if sell_orders else None
        out, close_cash = self._exit_phase(pos, bar, rules, open_qty, afternoon=False, order=order)
        if late_qty > 0 and pos.symbol in self.positions:
            late, cash = self._exit_phase(pos, bar, rules, late_qty, afternoon=True, order=order)
            out += late
            close_cash += cash
        return out, close_cash

    def _exit_phase(self, pos: Position, bar: Bar, rules: _ExitRules, cap: int, *, afternoon: bool,
                    order: Order | None) -> tuple[list[Fill], float]:
        """Exit rules for ``cap`` shares: the whole session, or (``afternoon``) only its close."""
        if cap <= 0:
            return [], 0.0
        cash0 = self.cash
        if order is not None:
            raw = bar.close if afternoon else bar.open
            fill = self._sell(pos, cap, _fill_price(raw, "SELL", bar), bar.date, order.reason or "hệ thống chuyển SELL")
            return [fill], (self.cash - cash0 if afternoon else 0.0)
        if pos.stop and (bar.close <= pos.stop if afternoon else bar.low <= pos.stop):
            raw = bar.close if afternoon else min(bar.open, pos.stop)
            label = "chạm stop" if not pos.t1_done else "chạm stop hoà vốn sau T1"
            fill = self._sell(pos, cap, _fill_price(raw, "SELL", bar), bar.date, label)
            return [fill], (self.cash - cash0 if afternoon else 0.0)
        out: list[Fill] = []
        left = cap
        if pos.target1 and not pos.t1_done:
            price = _target_fill(pos.target1, bar, afternoon)
            if price is not None:
                part = vn_rules.round_lot(left * rules.t1_sell_fraction)
                if rules.t1_sell_fraction >= 1 or (part > 0 and left - part <= 0):
                    part = left
                if part > 0:
                    label = "chốt 1/2 tại T1" if rules.t1_sell_fraction == 0.5 else f"chốt {rules.t1_sell_fraction:.0%} tại T1"
                    out.append(self._sell(pos, part, price, bar.date, label))
                    left -= out[-1].qty
                if pos.symbol in self.positions:
                    pos.t1_done = True
                    pos.stop = max(pos.stop, pos.avg_price)
        if pos.symbol in self.positions and left > 0 and pos.t1_done and rules.use_t2 and pos.target2:
            price = _target_fill(pos.target2, bar, afternoon)
            if price is not None:
                out.append(self._sell(pos, left, price, bar.date, "chốt phần còn lại tại T2"))
                left -= out[-1].qty
        before_close = self.cash
        if pos.symbol in self.positions and left > 0 and pos.sessions >= rules.max_hold_sessions:
            out.append(self._sell(pos, left, _fill_price(bar.close, "SELL", bar), bar.date,
                                  f"hết {rules.max_hold_sessions} phiên nắm giữ"))
        return out, (self.cash - cash0 if afternoon else self.cash - before_close)

    # ── fills reported by the owner of a real account ──
    def settle_to(self, date: str) -> None:
        """Drop the lots that have had T+2 by ``date``, counting weekdays: for a book that
        no session processing ages, such as the real account. A lot bought on T counts
        as settled on T+2, whose afternoon can sell it. Lots not yet settled keep their
        own session count (no double counting when the book is also processed)."""
        for pos in self.positions.values():
            pos.unsettled = [lot for lot in pos.unsettled
                             if max(lot["sessions"], _sessions_between(lot["date"], date)) < SETTLEMENT_SESSIONS]

    def record_fill(self, side: str, symbol: str, qty: int, price: float, date: str, *,
                    stop: float | None = None, target1: float | None = None, target2: float | None = None,
                    reason: str = "báo khớp") -> Fill:
        """Book a fill the owner made at the broker. Raises ValueError (Vietnamese message) when it
        cannot have happened: not a positive multiple of 100 shares, a bad price or date, not enough
        cash for a BUY, or not enough settled shares (T+2) for a SELL.

        A BUY of a held symbol adds an unsettled lot and moves the average cost; the
        position keeps its stop/targets (``stop``/``target1``/``target2`` fill in missing ones).
        """
        side = str(side).strip().upper()
        symbol = str(symbol).strip().upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("Lệnh phải là MUA (BUY) hoặc BÁN (SELL).")
        if isinstance(qty, bool) or int(qty) != qty or qty <= 0 or int(qty) % vn_rules.LOT_SIZE:
            raise ValueError(f"Khối lượng phải là bội số dương của {vn_rules.LOT_SIZE} cổ phiếu.")
        qty = int(qty)
        if not finite(price) or float(price) <= 0:
            raise ValueError("Giá khớp không hợp lệ.")
        price = float(price)
        try:
            _date.fromisoformat(str(date)[:10])
        except ValueError:
            raise ValueError(f"Ngày khớp không hợp lệ: {date!r} (cần YYYY-MM-DD).") from None
        self.settle_to(date)
        if side == "BUY":
            value = qty * price
            fee = _fee(value, "BUY")
            if value + fee > self.cash + 0.5:
                raise ValueError(f"Không đủ tiền: lệnh cần {_vnd(value + fee)} VND (gồm phí), "
                                 f"tiền mặt còn {_vnd(self.cash)} VND. Cập nhật bằng /von nếu số dư đã đổi.")
            self.cash -= value + fee
            self._add_shares(symbol, qty, value + fee, date, stop=stop, target1=target1, target2=target2,
                             last_price=price)
            fill = Fill(date, symbol, "BUY", qty, price, round(fee), reason)
            self.fills.append(fill)
            return fill
        pos = self.positions.get(symbol)
        if pos is None or pos.qty < qty:
            raise ValueError(f"Tài khoản không có đủ {symbol} để bán (đang có {_vnd(pos.qty if pos else 0)} cp).")
        if pos.settled_qty < qty:
            raise ValueError(f"Mới có {_vnd(pos.settled_qty)}/{_vnd(pos.qty)} cp {symbol} về tài khoản: "
                             "cổ phiếu mua phiên T chỉ bán được từ chiều T+2.")
        fill = self._sell(pos, qty, price, date, reason)
        if symbol in self.positions and pos.target1 and not pos.t1_done and price >= pos.target1:
            # A part sold at T1: the plan's next step, as ``_exit_phase`` does for the paper
            # book (no second "sell half at T1", the rest protected at break-even).
            pos.t1_done = True
            pos.stop = max(pos.stop, pos.avg_price)
        return fill

    # ── orders for the next session ──
    def plan_orders(self, date: str, signals: list[Signal], *, max_positions: int) -> list[Order]:
        """Orders for the session after ``date`` from the engine's decisions at its close.

        No pyramiding: a held symbol gets no BUY, and a symbol with an open order of
        a side gets no second one (a session planned twice does not double the orders)."""
        new: list[Order] = []
        by_symbol = {s.symbol: s for s in signals}
        open_sells = {o.symbol for o in self.orders if o.side == "SELL"}
        open_buys = {o.symbol for o in self.orders if o.side == "BUY"}
        for symbol, pos in self.positions.items():
            sig = by_symbol.get(symbol)
            if sig is not None and sig.action == "SELL" and symbol not in open_sells:
                new.append(Order(symbol, "SELL", pos.qty, None, date, "hệ thống chuyển SELL"))
                open_sells.add(symbol)
        slots = max_positions - len(self.positions) - len(open_buys)
        nav = self.nav()
        available = self.cash - self.reserved()
        candidates = sorted(
            (s for s in signals if s.action == "BUY" and s.symbol not in self.positions
             and s.symbol not in open_buys and _positive(s.entry_high) and _positive(s.stop)
             and _positive(s.position_size_pct) and finite(s.confidence)),
            key=lambda s: s.confidence, reverse=True,
        )
        for sig in candidates:
            if slots <= 0:
                break
            if sig.symbol in open_buys:
                continue                      # the same symbol twice in the signals
            budget = min(nav * sig.position_size_pct / 100, available)
            qty = vn_rules.round_lot(budget / (sig.entry_high * (1 + vn_rules.BUY_FEE_PCT / 100)))
            if qty <= 0:
                continue
            new.append(Order(sig.symbol, "BUY", qty, sig.entry_high, date,
                             f"BUY conf {sig.confidence:.2f}", sig.stop, sig.target1, sig.target2,
                             ref=sig.price if _positive(sig.price) else None))
            open_buys.add(sig.symbol)
            available -= qty * sig.entry_high * (1 + vn_rules.BUY_FEE_PCT / 100)
            slots -= 1
        self.orders.extend(new)
        return new

    # ── stats ──
    def stats(self) -> dict:
        sells = [f for f in self.fills if f.side == "SELL" and f.pnl is not None]
        navs = [h["nav"] for h in self.history] or [self.start_cash]
        peak, max_dd = navs[0], 0.0
        for v in navs:
            peak = max(peak, v)
            max_dd = max(max_dd, (peak - v) / peak * 100 if peak else 0.0)
        bench = [h["benchmark"] for h in self.history if h.get("benchmark")]
        bench_ret = (bench[-1] / bench[0] - 1) * 100 if len(bench) >= 2 else None
        return {
            "nav": round(self.nav()),
            "return_pct": round((self.nav() / self.start_cash - 1) * 100, 2) if self.start_cash else 0.0,
            "benchmark_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "max_drawdown_pct": round(max_dd, 2),
            "trades": len({(f.symbol, f.date) for f in self.fills if f.side == "BUY"}),
            "closed": len(sells),
            "win_rate_pct": round(sum(1 for f in sells if f.pnl > 0) / len(sells) * 100, 1) if sells else None,
            "realised_pnl": round(sum(f.pnl for f in sells)),
        }

    # ── storage ──
    def to_dict(self) -> dict:
        return {
            "cash": self.cash, "start_cash": self.start_cash, "start_date": self.start_date,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "orders": [asdict(o) for o in self.orders],
            "fills": [asdict(f) for f in self.fills[-500:]],
            "history": self.history[-800:],
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Ledger:
        """A book from ``to_dict`` output. Fields added since a save take their defaults and
        fields this version does not know are ignored; a missing or non-finite ``cash``, or a
        record without its essential fields, is damage (KeyError/TypeError/ValueError)."""
        if not isinstance(data, dict):
            raise TypeError(f"a ledger is a JSON object, not {type(data).__name__}")
        cash = float(data["cash"])
        start_cash = float(data.get("start_cash", cash))
        if not (finite(cash) and finite(start_cash)):
            raise ValueError("non-finite cash")
        positions = data.get("positions") or {}
        if not isinstance(positions, dict):
            raise TypeError("positions must be an object")
        return cls(
            cash=cash, start_cash=start_cash, start_date=str(data.get("start_date") or ""),
            positions={k: _build(Position, v) for k, v in positions.items()},
            orders=[_build(Order, o) for o in data.get("orders") or []],
            fills=[_build(Fill, f) for f in data.get("fills") or []],
            history=[h for h in data.get("history") or [] if isinstance(h, dict) and "date" in h and "nav" in h],
            meta=dict(data.get("meta") or {}),
        )

    def save(self, path: Path) -> None:
        """Write atomically: a temporary file in the same folder, flushed to disk, then renamed over."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError, AttributeError):   # make the rename durable where the OS allows
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    @classmethod
    def load(cls, path: Path, start_cash: float, *, fresh_on_error: bool = False) -> Ledger:
        """The book saved at ``path``; a new one with ``start_cash`` only when there is no file.

        A file that exists but cannot be parsed is moved aside to
        ``<name>.corrupt-<timestamp>`` (never deleted) and ``LedgerLoadError`` is raised,
        so a damaged real account is never silently replaced by an empty book on the
        next save. ``fresh_on_error=True`` (for a throwaway book, e.g. paper) returns a
        new book instead, once the damaged file is safely aside. A file that cannot be
        read at all (permissions, I/O) is left in place and always raises.
        """
        path = Path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls(cash=start_cash, start_cash=start_cash)
        except OSError as exc:
            logger.error("Ledger %s cannot be read: %s", path, exc)
            raise LedgerLoadError(f"Không đọc được sổ giao dịch {path.name}: {exc}") from exc
        try:
            return cls.from_dict(json.loads(raw.decode("utf-8")))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            try:
                aside = _quarantine(path)
            except OSError as move_exc:
                logger.error("Ledger %s is damaged (%s) and cannot be moved aside: %s", path, exc, move_exc)
                raise LedgerLoadError(f"Sổ giao dịch {path.name} bị hỏng ({exc}) và không chuyển sang bên được; "
                                      "giữ nguyên file, cần kiểm tra thủ công.") from exc
            logger.error("Ledger %s is damaged (%s: %s); moved to %s", path, type(exc).__name__, exc, aside)
            if fresh_on_error:
                return cls(cash=start_cash, start_cash=start_cash)
            raise LedgerLoadError(f"Sổ giao dịch {path.name} bị hỏng ({type(exc).__name__}: {exc}); "
                                  f"đã chuyển sang {aside.name}, không xoá. Khôi phục file đó rồi chạy lại.") from exc

    def portfolio_context(self):
        """The book as the engine's PortfolioContext, so held stocks are judged as holdings."""
        from tradingagents.portfolio import PortfolioContext

        return PortfolioContext.model_validate({
            "cash": max(self.cash, 0.0), "currency": "VND",
            "positions": [{"ticker": p.symbol, "quantity": p.qty, "average_price": round(p.avg_price)}
                          for p in self.positions.values()],
        })


def _build(kind, raw):
    """A dataclass from a saved record: unknown keys dropped, missing ones defaulted."""
    if not isinstance(raw, dict):
        raise TypeError(f"{kind.__name__} record must be an object")
    names = {f.name for f in fields(kind)}
    return kind(**{k: v for k, v in raw.items() if k in names})


def _quarantine(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    path.rename(aside)
    return aside


def _positive(value) -> bool:
    return finite(value) and float(value) > 0


def finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
