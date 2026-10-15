"""The second strategy: momentum rotation over the VN30 basket.

Every ``mom_rebalance`` sessions the stocks are ranked by their return over
the last ``mom_lookback`` sessions and the top ``mom_top_k`` with a positive
return make the basket, equal weight. While VNINDEX closes under its
``mom_market_ma``-session average the basket is empty: everything is sold and
the book waits in cash; the day it closes back above, a new ranking is made.

Orders go at the next session's open through the same ledger as the Quant
Engine's book, with its rules (see ``ledger``): lots of 100, fees and tax,
slippage on every fill, no fills on limit-locked sessions, and T+2 - a stock
bought on T can leave on T+2 only at the close (a SELL order or the stop is
then filled at the close), from T+3 at the open. A stock that leaves the
basket is sold; one that stays is not resized, which keeps turnover (and
fees) down. ``mom_stop_pct`` adds a stop under each entry.

Since 2026-10-15 this is a comparison book: the real-account signals follow
the defensive trend book (``factor``) by default. The independent evaluation of
2026-10-15 (data to 2026-10-14, all costs, next-open fills, T+2) ran this rule
with the default parameters on point-in-time universes. CAGR / max drawdown:

    universe                          TRAIN 2013-20   VALID 2021-23   TEST 2024-10/2026   2013-10/2026
    VN30 proxy (30 largest liquid caps)  4.3% / -43.9%   2.9% / -46.4%   11.8% / -33.1%      4.7% / -45.2%
    50 largest liquid caps              10.7% / -48.9%  13.4% / -47.5%    7.5% / -41.3%     10.9% / -48.9%
    today's VN30 (biased)               10.9% / -41.5%  -2.1% / -55.0%   26.2% / -22.1%     11.6% / -53.0%

The +73-91% reported earlier for 2022-2026 was measured on today's VN30, which
holds stocks that were on UPCoM when the rule bought them (MCH, BSR): the
survivorship flattered it. Past results do not promise future ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradingagents.quant.params import Params
from tradingagents.trading.ledger import Ledger, Order, _ExitRules

# A momentum position is held until it leaves the basket: no T1/T2, no time stop.
RULES = _ExitRules(max_hold_sessions=10**9, t1_sell_fraction=0.0, use_t2=False, trail_pct=0.0)


@dataclass
class Decision:
    market_ok: bool
    rebalanced: bool
    basket: list[str]
    ranking: list[tuple[str, float]] = field(default_factory=list)   # (symbol, return %), best first
    note: str = ""


def market_ok(vni_closes: list[float], ma: int) -> bool:
    if len(vni_closes) < ma:
        return False
    return vni_closes[-1] >= sum(vni_closes[-ma:]) / ma


def rank(closes: dict[str, list[float]], lookback: int) -> list[tuple[str, float]]:
    """Symbols by return over ``lookback`` sessions, best first (those with too short a history left out)."""
    out = []
    for symbol, series in closes.items():
        if len(series) > lookback and series[-1 - lookback] > 0:
            out.append((symbol, round((series[-1] / series[-1 - lookback] - 1) * 100, 2)))
    return sorted(out, key=lambda x: x[1], reverse=True)


def decide(ledger: Ledger, closes: dict[str, list[float]], vni_closes: list[float], params: Params) -> Decision:
    """Today's basket; updates the strategy state kept in ``ledger.meta``."""
    meta = ledger.meta
    ok = market_ok(vni_closes, params.mom_market_ma)
    since = int(meta.get("mom_since", 10**6)) + 1
    was_ok = bool(meta.get("mom_market_ok", False))
    if not ok:
        meta.update(mom_basket=[], mom_since=since, mom_market_ok=False)
        return Decision(False, False, [], note=f"VNINDEX dưới MA{params.mom_market_ma} - giữ tiền mặt")
    if since >= params.mom_rebalance or not was_ok or "mom_basket" not in meta:
        ranking = rank(closes, params.mom_lookback)
        basket = [s for s, r in ranking if r > 0][: params.mom_top_k]
        meta.update(mom_basket=basket, mom_since=0, mom_market_ok=True)
        return Decision(True, True, basket, ranking, note=f"xếp hạng lại theo lợi nhuận {params.mom_lookback} phiên")
    meta.update(mom_since=since, mom_market_ok=True)
    return Decision(True, False, list(meta.get("mom_basket") or []))


def plan(ledger: Ledger, day: str, decision: Decision, last_close: dict[str, float], params: Params) -> list[Order]:
    """Orders at the next open that move the book to the basket."""
    orders: list[Order] = []
    basket = set(decision.basket)
    proceeds = 0.0
    # A symbol that already has an open order of a side gets no second one (a day planned twice).
    open_sells = {o.symbol for o in ledger.orders if o.side == "SELL"}
    open_buys = {o.symbol for o in ledger.orders if o.side == "BUY"}
    for symbol, pos in ledger.positions.items():
        if symbol not in basket and symbol not in open_sells:
            reason = "VNINDEX dưới MA - về tiền mặt" if not decision.market_ok else "rời rổ động lượng"
            orders.append(Order(symbol, "SELL", pos.qty, None, day, reason))
            proceeds += pos.qty * (last_close.get(symbol) or pos.last_price)
    nav = ledger.nav()
    available = ledger.cash - ledger.reserved() + proceeds * 0.997
    from tradingagents.market import vn_rules

    weight = 1 / max(params.mom_top_k, 1)
    for symbol in decision.basket:
        if symbol in ledger.positions or symbol in open_buys or not last_close.get(symbol):
            continue
        price = last_close[symbol]
        budget = min(nav * weight, available)
        qty = vn_rules.round_lot(budget / (price * (1 + vn_rules.BUY_FEE_PCT / 100) * 1.02))  # room for the open
        if qty <= 0:
            continue
        orders.append(Order(symbol, "BUY", qty, None, day, "vào rổ động lượng", ref=price,
                            stop_pct=params.mom_stop_pct))
        available -= qty * price * 1.02
    ledger.orders.extend(orders)
    return orders


def run(data, params: Params, *, start: str | None = None, end: str | None = None,
        start_cash: float = 1_000_000_000.0) -> Ledger:
    """Backtest over a simulator.Dataset (its bars and VNINDEX closes)."""
    ledger = Ledger(cash=start_cash, start_cash=start_cash)
    days = [d for d in data.dates if (start is None or d >= start) and (end is None or d <= end)]
    vni_days = sorted(data.benchmark)
    vni_values = [data.benchmark[d] for d in vni_days]
    import bisect

    series = {s: (sorted(bars), bars) for s, bars in data.bars.items()}
    for day in days:
        active = {o.symbol for o in ledger.orders} | set(ledger.positions)
        bars = {s: data.bars[s][day] for s in active if day in data.bars.get(s, {})}
        ledger.process_session(bars, rules=RULES)
        ledger.end_of_session(day, data.benchmark.get(day))
        need = params.mom_lookback + 1
        closes = {}
        for symbol, (dates, bars_by_day) in series.items():
            if day not in bars_by_day:
                continue
            i = bisect.bisect_right(dates, day)
            closes[symbol] = [bars_by_day[d].close for d in dates[max(0, i - need):i]]
        vni = vni_values[: bisect.bisect_right(vni_days, day)][-max(params.mom_market_ma, 1):]
        decision = decide(ledger, closes, vni, params)
        plan(ledger, day, decision, {s: c[-1] for s, c in closes.items()}, params)
    return ledger
