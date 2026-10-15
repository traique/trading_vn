"""Money-flow features: raw measurements, no thresholds.

Pure functions over the frames of ``dataflows.money_flows`` (already cut at
the run date). The thresholds that turn these numbers into a signal live in
``params.Params`` and are applied in ``policy.py``, so the calibration can
move them without recomputing anything here.

A series whose last row is more than ``STALE_DAYS`` calendar days before the
run date is treated as missing: an old flow reading is not today's flow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

STALE_DAYS = 7
# Market-wide foreign net (CafeF GTDGRong) includes negotiated deals and gives
# no matched-only split: one block deal can be 20x a normal session and set the
# sign of the 20-session sum by itself. A day is capped at this multiple of the
# median absolute daily net (sign kept, so sell-day counts do not change).
MARKET_OUTLIER_MULT = 5.0
_OUTLIER_BASE_DAYS = 60


@dataclass(frozen=True)
class StockFlows:
    sessions: int                      # rows the measurements rest on
    foreign_net_20: float              # VND
    foreign_ratio_20: float | None     # foreign net / matched value over 20 sessions, %
    foreign_streak: int                # consecutive net-buy sessions (>0) or net-sell (<0), latest first
    foreign_sell_days_10: int          # net-sell sessions among the last 10
    room_pct: float | None             # foreign room still open, % of shares
    prop_ratio_10: float | None        # proprietary net / matched value over 10 sessions, %
    last_date: str
    foreign_net_total_20: float | None = None  # VND incl. negotiated deals - display only


@dataclass(frozen=True)
class MarketFlows:
    foreign_net_20: float | None       # VND, whole market
    foreign_sell_days_15: int | None   # net-sell sessions among the last 15
    prop_net_20: float | None
    etf_net_20: float | None           # ETF net creations, VND
    adv_ratio_10: float | None         # advances / (advances + declines) over 10 sessions, %
    pct_above_ma50: float | None       # HOSE stocks above their 50-session average, %
    pct_above_ma50_change_10: float | None
    new_highs_20: int | None
    breadth_divergence: bool           # VNINDEX at a 20-session high while breadth weakens


def _fresh(frame: pd.DataFrame | None, as_of: str) -> bool:
    if frame is None or frame.empty or "Date" not in frame:
        return False
    return (pd.Timestamp(as_of) - pd.Timestamp(frame["Date"].iloc[-1])).days <= STALE_DAYS


def _finite(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _ratio(net: float, base: float) -> float | None:
    return round(net / base * 100, 2) if base and base > 0 else None


def _foreign_net_matched(frame: pd.DataFrame) -> pd.Series:
    """Foreign net on the order book only, falling back row by row to the total.

    ``ForeignNet`` includes negotiated (thỏa thuận) deals while ``MatchValue``
    does not: one block deal of several thousand billion VND would otherwise
    flip the 20-session ratio, the streak and the sell-day count. Cached rows
    written before the vendor exposed ``ForeignNetMatched`` keep the total."""
    total = frame["ForeignNet"].astype(float)
    if "ForeignNetMatched" not in frame:
        return total
    return frame["ForeignNetMatched"].astype(float).where(lambda v: v.notna(), total)


def stock_flows(frame: pd.DataFrame | None, as_of: str) -> StockFlows | None:
    """Foreign and proprietary activity in one stock over its recent sessions."""
    if not _fresh(frame, as_of) or len(frame) < 10:
        return None
    foreign = _foreign_net_matched(frame).fillna(0.0)
    tail20 = frame.tail(20)
    net = foreign.tail(20)
    match = tail20["MatchValue"].astype(float).fillna(0.0)
    signs = [1 if v > 0 else -1 if v < 0 else 0 for v in foreign]
    streak = 0
    for sign in reversed(signs):
        if sign == 0 or (streak and (sign > 0) != (streak > 0)):
            break
        streak += sign
    tail10 = frame.tail(10)
    prop_ratio = None
    if "PropNet" in frame and tail10["PropNet"].notna().sum() >= 5:
        prop_ratio = _ratio(float(tail10["PropNet"].astype(float).fillna(0).sum()),
                            float(tail10["MatchValue"].astype(float).fillna(0).sum()))
    return StockFlows(
        sessions=len(tail20),
        foreign_net_20=float(net.sum()),
        foreign_ratio_20=_ratio(float(net.sum()), float(match.sum())),
        foreign_streak=streak,
        foreign_sell_days_10=int((foreign.tail(10) < 0).sum()),
        room_pct=_finite(frame["ForeignRoomPct"].iloc[-1]) if "ForeignRoomPct" in frame else None,
        prop_ratio_10=prop_ratio,
        last_date=pd.Timestamp(frame["Date"].iloc[-1]).strftime("%Y-%m-%d"),
        foreign_net_total_20=float(tail20["ForeignNet"].astype(float).fillna(0.0).sum()),
    )


def _cap_outliers(series: pd.Series) -> pd.Series:
    """Clip single-day spikes (block deals) to MARKET_OUTLIER_MULT x the median |value|."""
    base = series.tail(_OUTLIER_BASE_DAYS).dropna().abs()
    if len(base) < 10:
        return series
    cap = float(base.median()) * MARKET_OUTLIER_MULT
    return series.clip(lower=-cap, upper=cap) if cap > 0 else series


def market_flows(
    flows: pd.DataFrame | None,
    etf: pd.DataFrame | None,
    breadth: pd.DataFrame | None,
    vni_closes: list[float] | None,
    as_of: str,
) -> MarketFlows | None:
    """The market's money flow and breadth; None when none of it is available."""
    foreign_net = sell_days = prop_net = etf_net = None
    if _fresh(flows, as_of) and len(flows) >= 15:
        fnet = flows["ForeignNet"].astype(float)
        foreign_net = float(_cap_outliers(fnet).tail(20).fillna(0).sum())
        sell_days = int((fnet.tail(15) < 0).sum())
        if flows["PropNet"].tail(20).notna().sum() >= 10:
            prop_net = float(flows["PropNet"].astype(float).tail(20).fillna(0).sum())
    if _fresh(etf, as_of) and len(etf) >= 10:
        etf_net = float(etf["EtfNet"].astype(float).tail(20).fillna(0).sum())

    adv_ratio = pct50 = pct50_change = None
    new_highs = None
    divergence = False
    if _fresh(breadth, as_of) and len(breadth) >= 10:
        tail = breadth.tail(10)
        adv, dec = float(tail["Advances"].sum()), float(tail["Declines"].sum())
        adv_ratio = round(adv / (adv + dec) * 100, 1) if adv + dec > 0 else None
        if "PctAboveMA50" in breadth:
            series = breadth["PctAboveMA50"].astype(float)
            pct50 = _finite(series.iloc[-1])
            if len(series) > 10:
                before = _finite(series.iloc[-11])
                if pct50 is not None and before is not None:
                    pct50_change = round(pct50 - before, 1)
        if "NewHighs20" in breadth:
            new_highs = int(breadth["NewHighs20"].iloc[-1]) if pd.notna(breadth["NewHighs20"].iloc[-1]) else None
        if vni_closes and len(vni_closes) >= 20:
            at_high = vni_closes[-1] >= max(vni_closes[-20:])
            weakening = (pct50_change is not None and pct50_change <= -5) or (adv_ratio is not None and adv_ratio < 45)
            divergence = bool(at_high and weakening)

    if all(v is None for v in (foreign_net, etf_net, adv_ratio, pct50)):
        return None
    return MarketFlows(
        foreign_net_20=foreign_net, foreign_sell_days_15=sell_days, prop_net_20=prop_net,
        etf_net_20=etf_net, adv_ratio_10=adv_ratio, pct_above_ma50=pct50,
        pct_above_ma50_change_10=pct50_change, new_highs_20=new_highs, breadth_divergence=divergence,
    )


def fmt_bn(value: float | None) -> str:
    """VND as 'tỷ' with sign, e.g. -1.234,5 tỷ."""
    if value is None:
        return "N/A"
    text = f"{value / 1e9:+,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} tỷ"
