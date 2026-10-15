"""Point-in-time stock universes.

``universe(date, kind)`` uses only data from sessions strictly before ``date``:

- ``"vn30_today"``: today's VN30 members (survivorship- and look-ahead-biased;
  for comparison with the repo's reported numbers only).
- ``"liq_top30" / "liq_top50" / "liq_top100"`` (any ``"liq_topN"``): the N HOSE
  stocks with the highest median traded value (VND, matched) over the last 60
  sessions, among stocks that
    * traded on HOSE on the last session before ``date`` (board inferred per
      day from the price band, so a stock that moved HNX->HOSE counts only from
      its move, and one demoted to UPCoM drops out),
    * have >= 250 sessions of bars,
    * have an UNADJUSTED close >= 5,000đ on their last bar,
    * traded in at least one of the last 5 sessions.
  Sessions without a bar count as zero traded value in the median (a suspended
  stock is illiquid).

Delisted stocks are included where the vendors still serve their history
(VCI does for most; see README "Data coverage").
"""

from __future__ import annotations

import weakref

import numpy as np
import pandas as pd

from research.data import Panel, load_panel

MIN_HISTORY = 250
MIN_PRICE = 5_000.0
LIQ_WINDOW = 60
RECENT = 5

# id(panel) -> (weak reference to the panel, its eligibility/median arrays). The weak
# reference ties each entry to the panel object itself: CPython reuses the id of a
# collected panel, so an id alone could hand a new panel the old one's universe.
# Entries go away with their panel.
_cache: dict[int, tuple[weakref.ref, dict]] = {}


def _forget(key: int, ref: weakref.ref) -> None:
    entry = _cache.get(key)
    if entry is not None and entry[0] is ref:
        del _cache[key]


def _prep(panel: Panel) -> dict:
    key = id(panel)
    entry = _cache.get(key)
    if entry is not None and entry[0]() is panel:
        return entry[1]
    has = panel.close.notna()
    nbars = has.cumsum()
    listed = nbars > 0
    val = panel.value.where(has, 0.0).where(listed)
    med = val.rolling(LIQ_WINDOW, min_periods=LIQ_WINDOW // 2).median()
    raw = panel.raw_close.where(panel.raw_close.notna(), panel.close.where(panel.raw_close.isna() & has))
    raw_last = raw.ffill()
    recent = has.astype(float).rolling(RECENT, min_periods=1).max() > 0
    hose = panel.exchange == "HOSE"
    eligible = hose & (nbars >= MIN_HISTORY) & (raw_last >= MIN_PRICE) & recent & med.notna() & (med > 0)
    prep = {"med": med.to_numpy(), "eligible": eligible.to_numpy(), "cols": np.array(panel.close.columns),
            "out": {}}
    _cache[key] = (weakref.ref(panel, lambda ref, key=key: _forget(key, ref)), prep)
    return prep


def universe(date, kind: str = "liq_top30", panel: Panel | None = None) -> list[str]:
    """Symbols of ``kind`` for trading on ``date``, from data strictly before ``date``."""
    panel = panel or load_panel()
    if kind == "vn30_today":
        return list(panel.vn30_today)
    if not kind.startswith("liq_top"):
        raise ValueError(f"unknown universe kind {kind!r}")
    n = int(kind[len("liq_top"):])
    i = int(panel.dates.searchsorted(pd.Timestamp(date), side="left")) - 1
    if i < 0:
        return []
    prep = _prep(panel)
    hit = prep["out"].get((i, n))
    if hit is not None:
        return list(hit)
    ok = prep["eligible"][i]
    med = np.where(ok, prep["med"][i], -np.inf)
    order = np.argsort(-med, kind="stable")[:n]
    out = [str(prep["cols"][j]) for j in order if ok[j]]
    prep["out"][(i, n)] = out
    return list(out)


def universe_history(kind: str, start: str, end: str, freq: str = "MS", panel: Panel | None = None) -> pd.Series:
    """The universe on the first session of each period (for inspection)."""
    panel = panel or load_panel()
    days = panel.dates[(panel.dates >= start) & (panel.dates <= end)]
    firsts = pd.Series(days, index=days).groupby(days.to_period("M")).min()
    return pd.Series({d: universe(d, kind, panel) for d in firsts})
