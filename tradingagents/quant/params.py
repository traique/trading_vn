"""The Quant Engine's tunable thresholds, and the set currently in force.

``Params()`` holds the defaults: the thresholds the policy was written with
(identical to the constants in ``policy.py``) plus the money-flow ones. The
nightly calibration (``tradingagents.trading.calibrate``) may move a tunable
field one step at a time within its bounds; the set in force is written to
``quant_params.json`` and read back here. Without that file every run uses
the defaults, so a fresh install behaves exactly as written.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

logger = logging.getLogger(__name__)

PARAMS_FILE = "quant_params.json"


@dataclass(frozen=True)
class Params:
    # ── Policy gates (defaults = the original constants) ──
    confidence_buy_min: float = 0.75
    confidence_watch_min: float = 0.55
    min_rr_ratio: float = 1.5
    distribution_day_threshold: int = 4
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 20.0
    # ── Money flows ──
    # Confidence moved by a full-strength flow signal (0 = flows ignored).
    flow_weight: float = 0.05
    # Foreign net value over 20 sessions, % of matched value, that counts as
    # accumulation (or, negated, distribution).
    foreign_ratio_min: float = 3.0
    # Consecutive foreign net-buy sessions that confirm accumulation.
    foreign_streak_min: int = 4
    # Proprietary net value over 10 sessions, % of matched value, that confirms foreigners.
    prop_ratio_min: float = 2.0
    # Market-wide foreign net-sell sessions out of 15 that lower the regime one notch.
    market_foreign_sell_days: int = 11
    # 1 = a VNINDEX 20-session high on weakening breadth lowers the regime one notch.
    breadth_divergence_on: int = 1
    # ── Strategy variants (defaults = the original behaviour) ──
    # A distribution day: VNINDEX down at least this % on higher volume (O'Neil: 0.2).
    dist_min_drop_pct: float = 0.2
    # A distribution day stops counting once VNINDEX closes this % above it (O'Neil: 5; 0 = never).
    dist_expiry_rally_pct: float = 0.0
    # 1 = take the first resistance that gives the minimum R:R instead of always the nearest.
    skip_near_resistance: int = 0
    # 1 = a breakout with no resistance above may BUY, target = entry + projected_rr × risk.
    breakout_projected_ok: int = 0
    projected_rr: float = 2.0
    # ── Trading (paper and signals) ──
    max_positions: int = 5
    max_hold_sessions: int = 30
    # Share sold at T1 (the rest runs to T2, or the trailing stop).
    t1_sell_fraction: float = 0.5
    # 1 = sell the rest at T2; 0 = let it run on the trailing stop.
    use_t2: int = 1
    # After T1, the stop trails this % under the highest close (0 = break-even only).
    trail_pct: float = 0.0
    # ── Momentum rotation (the second strategy, trading/momentum.py) ──
    mom_top_k: int = 5                 # stocks held, equal weight
    mom_lookback: int = 63             # sessions of return that rank them (~3 months)
    mom_market_ma: int = 100           # all in cash while VNINDEX closes under this average
    mom_rebalance: int = 21            # sessions between two rankings (~1 month)
    mom_stop_pct: float = 0.0          # per-stock stop under the entry, % (0 = none)


# What the calibration may change, and how: (low, high, step).
# The Quant Engine's (and its trading rules') thresholds...
TUNABLE: dict[str, tuple[float, float, float]] = {
    "confidence_buy_min": (0.60, 0.85, 0.025),
    "min_rr_ratio": (1.2, 2.5, 0.1),
    "distribution_day_threshold": (3, 7, 1),
    "flow_weight": (0.0, 0.15, 0.025),
    "foreign_ratio_min": (1.0, 10.0, 1.0),
    "foreign_streak_min": (2, 8, 1),
    "prop_ratio_min": (0.5, 6.0, 0.5),
    "market_foreign_sell_days": (8, 15, 1),
    "breadth_divergence_on": (0, 1, 1),
    "max_hold_sessions": (10, 60, 5),
    "dist_min_drop_pct": (0.2, 1.5, 0.1),
    "dist_expiry_rally_pct": (0.0, 8.0, 1.0),
    "projected_rr": (1.5, 3.5, 0.25),
    "trail_pct": (0.0, 15.0, 1.0),
    "t1_sell_fraction": (0.0, 1.0, 0.25),
    "risk_per_trade_pct": (0.5, 2.5, 0.25),
    "max_positions": (3, 10, 1),
}
# ...and the momentum rotation's, calibrated separately.
MOMENTUM_TUNABLE: dict[str, tuple[float, float, float]] = {
    "mom_top_k": (3, 8, 1),
    "mom_lookback": (42, 147, 21),
    "mom_market_ma": (50, 200, 25),
    "mom_rebalance": (10, 42, 5),
    "mom_stop_pct": (0.0, 20.0, 5.0),
}
BOUNDS = {**TUNABLE, **MOMENTUM_TUNABLE}

DEFAULT = Params()


def coerce(values: dict) -> Params:
    """A Params from a dict, ignoring unknown keys and clamping tunables to their bounds."""
    known = {f.name: f for f in fields(Params)}
    clean = {}
    for key, value in (values or {}).items():
        if key not in known:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if key in BOUNDS:
            low, high, _ = BOUNDS[key]
            value = min(max(value, low), high)
        default = getattr(DEFAULT, key)
        clean[key] = int(round(value)) if isinstance(default, int) else round(value, 4)
    return replace(DEFAULT, **clean)


def to_dict(params: Params) -> dict:
    return asdict(params)


def changed(params: Params, base: Params = DEFAULT) -> dict:
    """The fields of ``params`` that differ from ``base``, as {name: (old, new)}."""
    return {
        f.name: (getattr(base, f.name), getattr(params, f.name))
        for f in fields(Params)
        if getattr(base, f.name) != getattr(params, f.name)
    }


def _path() -> Path:
    explicit = os.environ.get("TRADINGAGENTS_PARAMS_PATH")
    if explicit:
        return Path(explicit)
    from tradingagents.dataflows.config import get_config

    return Path(get_config()["results_dir"]) / PARAMS_FILE


_lock = threading.Lock()
_cache: tuple[float, Params] | None = None


def active() -> Params:
    """The thresholds in force: ``quant_params.json`` if present, else the defaults."""
    global _cache
    path = _path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return DEFAULT
    with _lock:
        if _cache and _cache[0] == mtime:
            return _cache[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            params = coerce(data.get("params", data))
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable %s (%s); using the default thresholds", path, exc)
            params = DEFAULT
        _cache = (mtime, params)
        return params


def save(params: Params, note: str = "") -> Path:
    """Put ``params`` in force for every later run."""
    global _cache
    from datetime import datetime

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"params": to_dict(params), "saved_at": datetime.now().isoformat(timespec="seconds"), "note": note}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    with _lock:
        _cache = None
    return path
