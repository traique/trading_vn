"""Replay the Quant Engine over cached history and trade it with the ledger's rules.

``build_dataset`` reads the price bars and money flows once and computes the
policy inputs of every (stock, session) - the expensive part, independent of
the thresholds. ``run`` then decides and trades with any ``Params`` in a few
seconds, which is what lets the calibration try many candidate thresholds.

Known limits (said so in every calibration report): bars are adjusted for
corporate actions as of today; the basket is today's VN30 plus the
watchlist (survivorship); news is not replayed (its only effect, a nudge to a
projected target, is left out).
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field, replace

import pandas as pd

from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.ohlcv import load_ohlcv
from tradingagents.dataflows.symbols import exchange_of
from tradingagents.market import vn_rules
from tradingagents.quant import engine, flow_features as ff, policy
from tradingagents.quant.params import DEFAULT, Params
from tradingagents.trading.ledger import Bar, Ledger, Signal

logger = logging.getLogger(__name__)

START_CASH = 1_000_000_000.0
# Bars kept before the window: the momentum ranking looks back up to ~150 sessions.
WARMUP_BARS = 200


@dataclass
class Dataset:
    dates: list[str]                                   # sessions, oldest first
    inputs: dict[tuple[str, str], policy.PolicyInputs] = field(default_factory=dict)
    bars: dict[str, dict[str, Bar]] = field(default_factory=dict)
    benchmark: dict[str, float] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def _upto(frame: pd.DataFrame | None, dates: list, day: pd.Timestamp) -> pd.DataFrame | None:
    if frame is None or frame.empty:
        return None
    end = bisect.bisect_right(dates, day)
    return frame.iloc[:end]


def build_dataset(symbols: list[str], sessions: int, as_of: str, *, flows: bool = True) -> Dataset:
    """Policy inputs for every stock on each of the last ``sessions`` sessions up to ``as_of``."""
    from tradingagents.dataflows import money_flows as mf

    vni = load_ohlcv(vn_rules.BENCHMARK, as_of)
    vni_dates_ts = list(pd.to_datetime(vni["Date"]))
    window = vni_dates_ts[-sessions:]
    data = Dataset(dates=[d.strftime("%Y-%m-%d") for d in window])
    # Every VNINDEX close (not only the window): the momentum strategy reads its average.
    data.benchmark = {d.strftime("%Y-%m-%d"): float(c) for d, c in zip(vni_dates_ts, vni["Close"], strict=True)}

    market_by_day: dict[str, ff.MarketFlows | None] = {}
    if flows:
        def safe(load):
            try:
                return load(as_of, backfill=True)
            except Exception as exc:  # noqa: BLE001 - a missing series means no signal from it
                logger.warning("Calibration: %s unavailable: %s", getattr(load, "__name__", load), exc)
                return None
        market, etf, breadth = safe(mf.load_market_flows), safe(mf.load_etf_flows), safe(mf.load_breadth)
        md = list(market["Date"]) if market is not None and not market.empty else []
        ed = list(etf["Date"]) if etf is not None and not etf.empty else []
        bd = list(breadth["Date"]) if breadth is not None and not breadth.empty else []
        for day in window:
            key = day.strftime("%Y-%m-%d")
            vni_closes = [float(c) for c in vni["Close"].iloc[: bisect.bisect_right(vni_dates_ts, day)].tail(60)]
            market_by_day[key] = ff.market_flows(
                _upto(market, md, day), _upto(etf, ed, day), _upto(breadth, bd, day), vni_closes, key)
        try:
            members = set(mf.index_members("VN30"))
        except Exception:  # noqa: BLE001
            members = set()
    else:
        members = set()

    for symbol in symbols:
        try:
            frame = load_ohlcv(symbol, as_of)
        except VendorError as exc:
            data.skipped[symbol] = str(exc)
            continue
        dates_ts = list(pd.to_datetime(frame["Date"]))
        exchange = exchange_of(symbol)
        stock_flow_frame = None
        if flows:
            try:
                stock_flow_frame = mf.load_stock_flows(symbol, as_of, backfill=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Calibration: flows of %s unavailable: %s", symbol, exc)
        fd = list(stock_flow_frame["Date"]) if stock_flow_frame is not None and not stock_flow_frame.empty else []
        bars: dict[str, Bar] = {}
        closes = frame["Close"].astype(float).tolist()
        first = max(0, bisect.bisect_left(dates_ts, window[0]) - WARMUP_BARS)
        for i, day in enumerate(dates_ts):
            if i < first:
                continue
            row = frame.iloc[i]
            bars[day.strftime("%Y-%m-%d")] = Bar(
                day.strftime("%Y-%m-%d"), float(row["Open"]), float(row["High"]), float(row["Low"]),
                float(row["Close"]), closes[i - 1] if i else None)
        data.bars[symbol] = bars
        for day in window:
            key = day.strftime("%Y-%m-%d")
            if key not in bars:
                continue                       # suspended that day: no decision
            end = bisect.bisect_right(dates_ts, day)
            if end < 60:
                continue
            stock = ff.stock_flows(_upto(stock_flow_frame, fd, day), key) if fd else None
            try:
                prep = engine.build_inputs(
                    symbol, key, frame.iloc[max(0, end - engine.HISTORY_BARS):end],
                    vni.iloc[: bisect.bisect_right(vni_dates_ts, day)],
                    exchange=exchange, source=frame.attrs.get("source", ""),
                    stock_flows=stock, market_flows=market_by_day.get(key),
                    etf_member=symbol in members, params=DEFAULT,
                )
            except Exception as exc:  # noqa: BLE001 - one bad day must not sink the dataset
                logger.debug("Calibration: %s %s skipped: %s", symbol, key, exc)
                continue
            data.inputs[(symbol, key)] = prep.inputs
        data.symbols.append(symbol)
    return data


def _signal(symbol: str, inputs: policy.PolicyInputs) -> Signal:
    d = policy.evaluate_policy(inputs)
    plan = d.trade_plan
    return Signal(
        symbol=symbol, action=d.action, confidence=d.confidence, price=inputs.price,
        entry_low=plan.entry_low if plan else None, entry_high=plan.entry_high if plan else None,
        stop=d.stop_price, target1=d.target_price, target2=plan.target2 if plan else None,
        position_size_pct=plan.position_size_pct if plan else None,
    )


def run(data: Dataset, params: Params, *, start: str | None = None, end: str | None = None,
        start_cash: float = START_CASH) -> Ledger:
    """Trade the window [start, end] of the dataset with ``params``; returns the finished book."""
    ledger = Ledger(cash=start_cash, start_cash=start_cash)
    days = [d for d in data.dates if (start is None or d >= start) and (end is None or d <= end)]
    for day in days:
        active = {o.symbol for o in ledger.orders} | set(ledger.positions)
        bars = {s: data.bars[s][day] for s in active if day in data.bars.get(s, {})}
        ledger.process_session(bars, params=params)
        ledger.end_of_session(day, data.benchmark.get(day))
        signals = []
        for symbol in data.symbols:
            base = data.inputs.get((symbol, day))
            if base is None:
                continue
            signals.append(_signal(symbol, replace(base, params=params, is_holding=symbol in ledger.positions)))
        ledger.plan_orders(day, signals, max_positions=params.max_positions)
    return ledger


@dataclass(frozen=True)
class Score:
    return_pct: float
    benchmark_pct: float | None
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float | None
    value: float              # what the calibration ranks on

    def line(self) -> str:
        bench = f", VNINDEX {self.benchmark_pct:+.2f}%" if self.benchmark_pct is not None else ""
        win = f", thắng {self.win_rate_pct:.0f}%" if self.win_rate_pct is not None else ""
        return (f"lãi {self.return_pct:+.2f}%{bench}, sụt giảm tối đa {self.max_drawdown_pct:.2f}%, "
                f"{self.trades} lệnh mua{win}")


DRAWDOWN_PENALTY = 0.5


def score(ledger: Ledger) -> Score:
    """Return minus half the max drawdown: a gain bought with deep drawdowns ranks lower."""
    s = ledger.stats()
    return Score(
        return_pct=s["return_pct"], benchmark_pct=s["benchmark_pct"], max_drawdown_pct=s["max_drawdown_pct"],
        trades=s["trades"], win_rate_pct=s["win_rate_pct"],
        value=round(s["return_pct"] - DRAWDOWN_PENALTY * s["max_drawdown_pct"], 3),
    )
