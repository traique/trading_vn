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


def stock_flows(frame: pd.DataFrame | None, as_of: str) -> StockFlows | None:
    """Foreign and proprietary activity in one stock over its recent sessions."""
    if not _fresh(frame, as_of) or len(frame) < 10:
        return None
    tail20 = frame.tail(20)
    net = tail20["ForeignNet"].astype(float).fillna(0.0)
    match = tail20["MatchValue"].astype(float).fillna(0.0)
    signs = [1 if v > 0 else -1 if v < 0 else 0 for v in frame["ForeignNet"].astype(float).fillna(0.0)]
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
        foreign_sell_days_10=int((tail10["ForeignNet"].astype(float) < 0).sum()),
        room_pct=_finite(frame["ForeignRoomPct"].iloc[-1]) if "ForeignRoomPct" in frame else None,
        prop_ratio_10=prop_ratio,
        last_date=pd.Timestamp(frame["Date"].iloc[-1]).strftime("%Y-%m-%d"),
    )


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
        foreign_net = float(fnet.tail(20).fillna(0).sum())
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
