"""The repo's Quant Engine as a harness strategy (expensive: build the dataset once).

``build_dataset(symbols, start, end)`` calls ``simulator.build_dataset`` with
its ``load_ohlcv`` pointed at the harness cache (the repo loader keeps only 5
years, too short for 2021-2023) and ``flows=False`` (money flows - foreign,
proprietary, ETF, breadth - are NOT replayed: the engine sees no flow signal).
``QuantEngine(dataset)`` then trades it exactly like ``simulator.run``:
``ledger.plan_orders`` with the engine's signals, exits by ``_ExitRules.of(params)``.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from research.config import LOCK_AFTER, OHLCV_DIR
from tradingagents.quant.params import DEFAULT, Params
from tradingagents.trading import simulator as sim
from tradingagents.trading.ledger import _ExitRules


def _cached_load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True, sources=None) -> pd.DataFrame:
    from tradingagents.dataflows.errors import NoMarketDataError

    path = OHLCV_DIR / f"{symbol}.csv"
    if not path.exists():
        raise NoMarketDataError(symbol, symbol, "not in the harness cache")
    f = pd.read_csv(path)
    f["Date"] = pd.to_datetime(f["Date"])
    f = f[(f["Date"] <= pd.Timestamp(as_of_date)) & (f["Close"] > 0)].reset_index(drop=True)
    if f.empty:
        raise NoMarketDataError(symbol, symbol, "no bars")
    if fill_gaps:
        cols = ["Open", "High", "Low", "Close", "Volume"]
        f[cols] = f[cols].ffill().bfill()
    f.attrs["source"] = "vci"
    return f


def build_dataset(symbols: list[str], start: str, end: str, *, unlock_test: bool = False) -> sim.Dataset:
    if pd.Timestamp(end) > pd.Timestamp(LOCK_AFTER) and not unlock_test:
        raise RuntimeError("TEST period is locked")
    vni = _cached_load_ohlcv("VNINDEX", end)
    sessions = int((vni["Date"] >= pd.Timestamp(start)).sum())
    orig = sim.load_ohlcv
    sim.load_ohlcv = _cached_load_ohlcv
    try:
        return sim.build_dataset(symbols, sessions, end, flows=False)
    finally:
        sim.load_ohlcv = orig


class QuantEngine:
    def __init__(self, data: sim.Dataset, params: Params = DEFAULT, name: str = "quant_engine"):
        self.data, self.params, self.name = data, params, name
        self.rules = _ExitRules.of(params, 30)

    def on_session(self, ctx):
        signals = []
        for symbol in self.data.symbols:
            base = self.data.inputs.get((symbol, ctx.day))
            if base is None:
                continue
            signals.append(sim._signal(symbol, replace(base, params=self.params,
                                                       is_holding=symbol in ctx.ledger.positions)))
        ctx.ledger.plan_orders(ctx.day, signals, max_positions=self.params.max_positions)
        return None
