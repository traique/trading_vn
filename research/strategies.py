"""Baseline strategies."""

from __future__ import annotations

from tradingagents.quant.params import DEFAULT, Params
from tradingagents.trading import momentum


class RepoMomentum:
    """The repo's VN30 momentum rotation, run by its OWN ``momentum.decide`` and
    ``momentum.plan`` (same as ``momentum.run``), on a point-in-time universe.

    Each session: closes over each symbol's own last ``mom_lookback + 1`` bars
    (symbols without a bar today are left out, as ``momentum.run`` does), VNINDEX
    closes for the MA filter, then decide (state in ``ledger.meta``) and plan
    (orders appended to ``ledger.orders``, picked up by the harness).
    """

    rules = momentum.RULES

    def __init__(self, params: Params = DEFAULT, name: str = "repo_momentum"):
        self.params = params
        self.name = name

    def on_session(self, ctx):
        p = self.params
        syms = ctx.universe()
        closes = ctx.closes_by_bar(syms, p.mom_lookback + 1)
        vni = ctx.vnindex.iloc[-max(p.mom_market_ma, 1):].tolist()
        decision = momentum.decide(ctx.ledger, closes, vni, p)
        last = {s: c[-1] for s, c in closes.items()}
        for s in ctx.ledger.positions:          # held names that left the universe still need a price
            if s not in last and ctx.last_close(s):
                last[s] = ctx.last_close(s)
        momentum.plan(ctx.ledger, ctx.day, decision, last, p)
        return None


class EqualWeightRebalance:
    """Equal weight over the universe, reconstituted on the first session of each month.

    Held names that stay in the universe are not resized (the ledger enters once
    and exits fully), so weights drift between reconstitutions.
    """

    def __init__(self, name: str = "ew_monthly"):
        self.name = name

    def on_session(self, ctx):
        month = (ctx.date.year, ctx.date.month)
        nxt = ctx._panel.dates[ctx.i + 1] if ctx.i + 1 < len(ctx._panel.dates) else None
        # Decide on the last session of a month: orders fill on the month's first session.
        if nxt is not None and (nxt.year, nxt.month) == month and ctx.state.get("done"):
            return None
        ctx.state["done"] = True
        syms = [s for s in ctx.universe() if ctx.last_close(s)]
        if not syms:
            return None
        w = 0.98 / len(syms)
        return dict.fromkeys(syms, w)


class BuyHoldSymbol:
    """Buy one symbol at the first opportunity and hold it (for tests)."""

    def __init__(self, symbol: str, weight: float = 0.99):
        self.symbol, self.weight, self.name = symbol, weight, f"buyhold_{symbol}"

    def on_session(self, ctx):
        return {self.symbol: self.weight}


# ── strategies by name (``python -m research run <name>``) ───────────────────

STRATEGIES = {
    "factor": "the bot's default book 'Phòng thủ' (tradingagents.trading.factor, the live module; own cap50 universe)",
    "momentum": "the repo's momentum rotation (momentum.decide/plan) on --universe",
    "ew": "equal weight over --universe, reconstituted at each month end",
    "quant": "the Quant Engine (simulator signals, money flows NOT replayed) on today's VN30 - slow",
    "vnindex": "VNINDEX buy & hold (price index, no costs)",
    "buyhold:SYM": "buy one symbol at the first session and hold it",
}


def make(name: str, *, start: str | None = None, end: str | None = None, unlock_test: bool = False,
         panel=None, next_day: str = "calendar"):
    """A strategy object by name (see ``STRATEGIES``); "vnindex" returns None (``backtest.run_index``)."""
    key = name.lower()
    if key == "factor":
        from research.factor_book import DefensiveBook

        return DefensiveBook(next_day=next_day)
    if key == "momentum":
        return RepoMomentum()
    if key == "ew":
        return EqualWeightRebalance()
    if key == "vnindex":
        return None
    if key.startswith("buyhold:"):
        symbol = name.split(":", 1)[1].strip().upper()
        if not symbol:
            raise ValueError("buyhold needs a symbol: buyhold:FPT")
        if panel is not None and symbol not in panel.close.columns:
            raise ValueError(f"unknown symbol {symbol!r}: not in the research data "
                             f"(`python -m research download --symbols {symbol}` fetches it)")
        return BuyHoldSymbol(symbol)
    if key == "quant":
        from research.quant_engine import QuantEngine, build_dataset

        if panel is None:
            from research.data import load_panel

            panel = load_panel(unlock_test=unlock_test)
        data = build_dataset(list(panel.vn30_today), start, end, unlock_test=unlock_test)
        engine = QuantEngine(data)
        engine.universe_label = "vn30_today"
        return engine
    raise ValueError(f"unknown strategy {name!r}; known: {', '.join(STRATEGIES)}")
