"""The bot's default strategy ("Phòng thủ", ``tradingagents.trading.factor``) as a harness strategy.

Nothing of the strategy is copied here: every decision is taken by the LIVE
module, called the way ``vnbot.trader.TradingBot._factor_session`` calls it.
At the close of each session D:

1. ``factor.decide(book, D, next_day, VNINDEX closes, params, ranking)`` keeps
   its calendar and market-filter state in ``ledger.meta`` (fb_*);
2. when it rebuilds the basket it calls ``ranking()``, which here builds the
   point-in-time universe with ``factor.build_universe`` - fed from the panel
   instead of the vendors: the HOSE stocks of D (``panel.exchange``), their
   daily bars up to D and their VCI price-history rows up to D (matched value,
   unadjusted close, shares outstanding: ``panel.price_history``) - and scores
   it with ``factor.aligned_closes`` + ``factor.rank`` over the last 252
   VNINDEX sessions;
3. ``factor.plan`` writes the orders for D+1 on the ledger (the harness picks
   them up and fills them at the next open with the repo's ledger).

Like the bot, orders older than the previous session lapse (``expire_orders``).
``next_day="calendar"`` gives ``decide`` the real next session (what the bot
does when it replays past sessions, and what ``factor.run`` does);
``next_day="guess"`` gives it ``factor.next_session_guess`` (what the bot does
on the latest session; before 2026 the holiday table does not know every Tết,
so a week's check or a month's rebuild sometimes runs one session late).

Point in time: the per-symbol arrays below are built once from the whole
panel for speed, but every lookup is cut at session D (``bisect``), so the
module never sees a later row.
"""

from __future__ import annotations

import bisect
import math

import numpy as np

from tradingagents.quant.params import DEFAULT, Params
from tradingagents.trading import factor


class _Series:
    """One symbol's rows as parallel lists sorted by day (YYYY-MM-DD), cut by day."""

    __slots__ = ("days", "rows")

    def __init__(self, days: list[str], rows: list[tuple]):
        self.days, self.rows = days, rows

    def upto(self, day: str) -> dict:
        k = bisect.bisect_right(self.days, day)
        return dict(zip(self.days[:k], self.rows[:k], strict=True))


class _PanelData:
    """What ``factor.build_universe`` and ``factor.rank`` read, from a harness panel."""

    def __init__(self, panel):
        self.panel = panel
        self.days = [d.strftime("%Y-%m-%d") for d in panel.dates]
        self.close = panel.close.to_numpy()
        self.cols = {s: j for j, s in enumerate(panel.close.columns)}
        self.exchange = panel.exchange.to_numpy()
        volume = panel.volume.to_numpy()
        days = np.array(self.days)
        self.bars: dict[str, _Series] = {}
        for s, j in self.cols.items():
            has = ~np.isnan(self.close[:, j])
            self.bars[s] = _Series(days[has].tolist(),
                                   list(zip(self.close[has, j].tolist(), volume[has, j].tolist(), strict=True)))
        self.history: dict[str, _Series] = {}
        for s, frame in (panel.price_history or {}).items():
            f = frame.sort_index()
            self.history[s] = _Series(f.index.strftime("%Y-%m-%d").tolist(),
                                      list(zip(*(f[c].astype(float).tolist() for c in ("MatchValue", "RawClose", "Shares")),
                                               strict=True)))

    def hose(self, i: int) -> list[str]:
        row = self.exchange[i]
        return [s for s, j in self.cols.items() if row[j] == "HOSE"]

    def bars_of(self, symbol: str, day: str) -> dict:
        series = self.bars.get(symbol)
        return series.upto(day) if series is not None else {}

    def history_of(self, symbol: str, day: str) -> dict:
        series = self.history.get(symbol)
        return series.upto(day) if series is not None else {}

    def last_close(self, symbol: str, i: int) -> float | None:
        j = self.cols.get(symbol)
        if j is None:
            return None
        v = self.close[i, j]
        return None if math.isnan(v) else float(v)


class DefensiveBook:
    """``tradingagents.trading.factor`` run session by session as the bot runs it."""

    rules = factor.RULES
    expire_orders = True

    def __init__(self, params: Params = DEFAULT, *, next_day: str = "calendar", name: str = "factor"):
        if next_day not in ("calendar", "guess"):
            raise ValueError("next_day must be 'calendar' or 'guess'")
        self.params, self.next_day, self.name = params, next_day, name
        self.universe_label = f"cap{params.fb_universe}"
        self._data: _PanelData | None = None
        self.universes: dict[str, list[str]] = {}      # day -> cap universe, for inspection

    def _source(self, panel) -> _PanelData:
        if self._data is None or self._data.panel is not panel:
            self._data = _PanelData(panel)
        return self._data

    def universe(self, data: _PanelData, i: int) -> list[str]:
        """The cap universe on session i, by ``factor.build_universe`` (as the bot builds it)."""
        day = data.days[i]
        built = factor.build_universe(data.days[: i + 1], data.hose(i), self.params.fb_universe,
                                      bars_of=lambda s: data.bars_of(s, day),
                                      history_of=lambda s: data.history_of(s, day))
        self.universes[day] = list(built["universe"])
        return built["universe"]

    def ranking(self, data: _PanelData, i: int) -> list[factor.Ranked]:
        """``TradingBot._factor_ranking``: the universe's names with a bar on D, ranked."""
        names = [s for s in self.universe(data, i) if (c := data.last_close(s, i)) is not None and c > 0]
        window = data.days[max(0, i + 1 - factor.SCORE_WINDOW): i + 1]
        lo = i + 1 - len(window)
        closes = {}
        for s in names:
            col = data.close[lo: i + 1, data.cols[s]]
            closes[s] = factor.aligned_closes(dict(zip(window, col.tolist(), strict=True)), window)
        return factor.rank(closes, names)

    def on_session(self, ctx):
        data = self._source(ctx._panel)          # whole panel: every read below is cut at ctx.i
        i, day, p = ctx.i, ctx.day, self.params
        if self.next_day == "guess":
            next_day = factor.next_session_guess(day)
        else:
            next_day = data.days[i + 1] if i + 1 < len(data.days) else None   # calendar only, no prices
        vni = [float(c) for c in ctx.vnindex.iloc[-max(p.fb_market_ma, 1):]]
        decision = factor.decide(ctx.ledger, day, next_day, vni, p, lambda: self.ranking(data, i))
        last = {}
        for s in set(decision.basket) | set(ctx.ledger.positions):
            c = data.last_close(s, i)
            if c is not None and c > 0:
                last[s] = c
        factor.plan(ctx.ledger, day, decision, last, p)
        return None
