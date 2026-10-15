"""A trading book under Vietnamese rules, and the order/exit rules of the bot.

One set of rules serves the paper account and the backtest, so a backtest
result is what the paper account would have done:

- Decisions are taken on a session's close; orders go to the NEXT session.
- BUY: a limit order at the top of the engine's entry zone, good for one
  session. It fills when the session trades at or below the limit, at the
  open if the open is lower. Size is the engine's % of NAV, rounded down to
  lots of 100 and to the cash on hand. A one-price session that closes up
  (locked at the ceiling) fills no buy.
- Shares bought on T can be sold from T+2 (``SETTLEMENT_SESSIONS``).
- Exits, once sellable, in this order each session: a SELL order (the engine
  turned to SELL) at the open; the stop (at the open if it gapped below);
  ``t1_sell_fraction`` at T1 (then the stop moves to break-even, and trails
  ``trail_pct`` under the highest close if set) and the rest at T2 (unless
  ``use_t2`` is off); a time stop at the close after ``max_hold_sessions``. When a session touches both
  the stop and a target, the stop is assumed first. A one-price session that
  closes down (locked at the floor) fills no sell.
- Fees and the 0.1% sell tax from ``vn_rules`` are charged on every fill;
  the buy fee is part of the cost basis.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tradingagents.market import vn_rules

SETTLEMENT_SESSIONS = 2


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None = None

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
    buy_date: str
    stop: float
    target1: float | None
    target2: float | None
    sessions: int = 0          # sessions traded since the buy session
    t1_done: bool = False
    last_price: float = 0.0
    peak: float = 0.0          # highest close since the buy

    @property
    def sellable(self) -> bool:
        return self.sessions >= SETTLEMENT_SESSIONS


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
        return sum(o.qty * (o.limit or o.ref or 0) * (1 + vn_rules.BUY_FEE_PCT / 100)
                   for o in self.orders if o.side == "BUY")

    # ── one session ──
    def process_session(self, bars: dict[str, Bar], *, params=None, rules: _ExitRules | None = None) -> list[Fill]:
        """Apply one session to the whole book: every exit first (their cash pays
        for the buys, as brokers allow), then the buy orders."""
        rules = rules or _ExitRules.of(params, 30)
        fills: list[Fill] = []
        for symbol in list(self.positions):
            bar = bars.get(symbol)
            pos = self.positions[symbol]
            if bar is not None and bar.date > pos.buy_date:
                pos.sessions += 1
                fills += self._exits(pos, bar, rules)
        for order in list(self.orders):
            bar = bars.get(order.symbol)
            if bar is None or order.created >= bar.date:
                continue
            self.orders.remove(order)
            if order.side == "BUY" and order.symbol not in self.positions:
                fill = self._buy(order, bar)
                if fill:
                    fills.append(fill)
        for symbol, pos in self.positions.items():
            bar = bars.get(symbol)
            if bar is not None:
                self._mark(pos, bar, rules)
        return fills

    def _mark(self, pos: Position, bar: Bar, rules: _ExitRules) -> None:
        pos.last_price = bar.close
        pos.peak = max(pos.peak, bar.close)
        if pos.t1_done and rules.trail_pct:
            pos.stop = max(pos.stop, vn_rules.round_to_tick(pos.peak * (1 - rules.trail_pct / 100)))

    # ── one session for one symbol ──
    def process_bar(self, symbol: str, bar: Bar, *, max_hold_sessions: int = 30, params=None) -> list[Fill]:
        """Apply one session to this symbol's open orders and position. Returns the fills.

        ``params`` (quant.params.Params) sets the exit rules; ``max_hold_sessions``
        alone is kept for callers that pass only that."""
        fills: list[Fill] = []
        exit_rules = _ExitRules.of(params, max_hold_sessions)
        pos = self.positions.get(symbol)
        if pos is not None and bar.date > pos.buy_date:
            pos.sessions += 1
            fills += self._exits(pos, bar, exit_rules)
        for order in [o for o in self.orders if o.symbol == symbol and o.created < bar.date]:
            self.orders.remove(order)
            if order.side == "BUY" and symbol not in self.positions:
                fill = self._buy(order, bar)
                if fill:
                    fills.append(fill)
        pos = self.positions.get(symbol)
        if pos is not None:
            self._mark(pos, bar, exit_rules)
        return fills

    def end_of_session(self, date: str, benchmark: float | None = None) -> None:
        """Drop the orders that had their session (suspended symbols included) and record NAV."""
        self.orders = [o for o in self.orders if o.created >= date]
        if not self.start_date:
            self.start_date = date
        self.history = [h for h in self.history if h["date"] != date]
        self.history.append({"date": date, "nav": round(self.nav()), "benchmark": benchmark})

    def _buy(self, order: Order, bar: Bar) -> Fill | None:
        if bar.locked_up or (order.limit is not None and bar.low > order.limit):
            return None
        price = bar.open if order.limit is None else min(bar.open, order.limit)
        affordable = vn_rules.round_lot(self.cash / (price * (1 + vn_rules.BUY_FEE_PCT / 100)))
        qty = min(order.qty, affordable)
        if qty <= 0:
            return None
        value = qty * price
        fee = _fee(value, "BUY")
        self.cash -= value + fee
        self.positions[order.symbol] = Position(
            symbol=order.symbol, qty=qty, avg_price=(value + fee) / qty, buy_date=bar.date,
            stop=order.stop or (vn_rules.round_to_tick(price * (1 - order.stop_pct / 100)) if order.stop_pct else 0.0),
            target1=order.target1, target2=order.target2, last_price=bar.close, peak=bar.close,
        )
        fill = Fill(bar.date, order.symbol, "BUY", qty, price, round(fee), order.reason or "mua theo tín hiệu")
        self.fills.append(fill)
        return fill

    def _sell(self, pos: Position, qty: int, price: float, date: str, reason: str) -> Fill:
        qty = min(qty, pos.qty)
        value = qty * price
        cost = _fee(value, "SELL")
        self.cash += value - cost
        pnl = value - cost - pos.avg_price * qty
        pos.qty -= qty
        if pos.qty <= 0:
            self.positions.pop(pos.symbol, None)
        fill = Fill(date, pos.symbol, "SELL", qty, price, round(cost), reason, round(pnl))
        self.fills.append(fill)
        return fill

    def _exits(self, pos: Position, bar: Bar, rules: _ExitRules) -> list[Fill]:
        if not pos.sellable or bar.locked_down:
            return []
        out: list[Fill] = []
        sell_orders = [o for o in self.orders if o.symbol == pos.symbol and o.side == "SELL" and o.created < bar.date]
        for order in sell_orders:
            self.orders.remove(order)
        if sell_orders:
            return [self._sell(pos, pos.qty, bar.open, bar.date, sell_orders[0].reason or "hệ thống chuyển SELL")]
        if pos.stop and bar.low <= pos.stop:
            price = min(bar.open, pos.stop)
            label = "chạm stop" if not pos.t1_done else "chạm stop hoà vốn sau T1"
            return [self._sell(pos, pos.qty, price, bar.date, label)]
        if pos.target1 and not pos.t1_done and bar.high >= pos.target1:
            part = vn_rules.round_lot(pos.qty * rules.t1_sell_fraction)
            if rules.t1_sell_fraction >= 1 or (part > 0 and pos.qty - part <= 0):
                part = pos.qty
            if part > 0:
                label = "chốt 1/2 tại T1" if rules.t1_sell_fraction == 0.5 else f"chốt {rules.t1_sell_fraction:.0%} tại T1"
                out.append(self._sell(pos, part, max(bar.open, pos.target1), bar.date, label))
            if pos.symbol in self.positions:
                pos.t1_done = True
                pos.stop = max(pos.stop, pos.avg_price)
        if (pos.symbol in self.positions and pos.t1_done and rules.use_t2
                and pos.target2 and bar.high >= pos.target2):
            out.append(self._sell(pos, pos.qty, max(bar.open, pos.target2), bar.date, "chốt phần còn lại tại T2"))
        if pos.symbol in self.positions and pos.sessions >= rules.max_hold_sessions:
            out.append(self._sell(pos, pos.qty, bar.close, bar.date, f"hết {rules.max_hold_sessions} phiên nắm giữ"))
        return out

    # ── orders for the next session ──
    def plan_orders(self, date: str, signals: list[Signal], *, max_positions: int) -> list[Order]:
        """Orders for the session after ``date`` from the engine's decisions at its close."""
        new: list[Order] = []
        by_symbol = {s.symbol: s for s in signals}
        for symbol, pos in self.positions.items():
            sig = by_symbol.get(symbol)
            if sig is not None and sig.action == "SELL":
                new.append(Order(symbol, "SELL", pos.qty, None, date, "hệ thống chuyển SELL"))
        slots = max_positions - len(self.positions) - sum(1 for o in self.orders if o.side == "BUY")
        nav = self.nav()
        available = self.cash - self.reserved()
        candidates = sorted(
            (s for s in signals if s.action == "BUY" and s.symbol not in self.positions
             and s.entry_high and s.stop and s.position_size_pct),
            key=lambda s: s.confidence, reverse=True,
        )
        for sig in candidates:
            if slots <= 0:
                break
            budget = min(nav * sig.position_size_pct / 100, available)
            qty = vn_rules.round_lot(budget / (sig.entry_high * (1 + vn_rules.BUY_FEE_PCT / 100)))
            if qty <= 0:
                continue
            new.append(Order(sig.symbol, "BUY", qty, sig.entry_high, date,
                             f"BUY conf {sig.confidence:.2f}", sig.stop, sig.target1, sig.target2))
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
        return cls(
            cash=float(data["cash"]), start_cash=float(data["start_cash"]), start_date=data.get("start_date", ""),
            positions={k: Position(**v) for k, v in (data.get("positions") or {}).items()},
            orders=[Order(**o) for o in data.get("orders") or []],
            fills=[Fill(**f) for f in data.get("fills") or []],
            history=list(data.get("history") or []),
            meta=dict(data.get("meta") or {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, start_cash: float) -> Ledger:
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return cls(cash=start_cash, start_cash=start_cash)

    def portfolio_context(self):
        """The book as the engine's PortfolioContext, so held stocks are judged as holdings."""
        from tradingagents.portfolio import PortfolioContext

        return PortfolioContext.model_validate({
            "cash": max(self.cash, 0.0), "currency": "VND",
            "positions": [{"ticker": p.symbol, "quantity": p.qty, "average_price": round(p.avg_price)}
                          for p in self.positions.values()],
        })


def finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
