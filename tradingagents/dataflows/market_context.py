"""The Vietnamese market backdrop at a date: indices, VNINDEX regime, sector rotation.

The market-level input of the news analyst: what moves a
Vietnamese stock day to day is the VNINDEX regime, the foreign flow and the
sector rotation, all computed here from closed sessions on or before the date.
"""

from __future__ import annotations

import logging

from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.ohlcv import load_ohlcv
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.market import vn_rules
from tradingagents.quant import features as feat, policy, report_format as rfmt, sector

logger = logging.getLogger(__name__)

INDICES = (("VNINDEX", "VN-Index"), ("VN30", "VN30"), ("HNX", "HNX-Index"), ("UPCOM", "UPCoM-Index"))
_REGIME_VI = {
    "risk_on": "risk-on (xu hướng chung tăng)",
    "neutral": "trung tính",
    "risk_off": "risk-off (thị trường chung xấu, hạn chế mua mới)",
    "unknown": "không xác định (thiếu dữ liệu)",
}


def get_market_context(ticker: str, as_of_date: str) -> str:
    """Index performance, VNINDEX regime and the stock's sector versus VNINDEX."""
    symbol = normalize_symbol(ticker)
    lines = [f"## Bối cảnh thị trường Việt Nam tới phiên đóng cửa gần nhất trước/đúng {as_of_date}", ""]
    vni_closes: list[float] = []
    for code, label in INDICES:
        try:
            frame = load_ohlcv(code, as_of_date)
        except VendorError as exc:
            lines.append(f"- {label}: không có dữ liệu ({exc})")
            continue
        closes = [float(c) for c in frame["Close"]]
        volumes = [float(v) for v in frame["Volume"]]
        tf = feat.calc_multi_timeframe(closes)
        last = frame["Date"].iloc[-1].strftime("%d/%m/%Y")
        lines.append(
            f"- {label} {rfmt.fmt_number(closes[-1])} điểm (phiên {last}): 1 tuần {rfmt.fmt_signed_pct(tf.trend_1w)}, "
            f"1 tháng {rfmt.fmt_signed_pct(tf.trend_1m)}, 3 tháng {rfmt.fmt_signed_pct(tf.trend_3m)} -> {tf.alignment}"
        )
        if code == "VNINDEX":
            vni_closes = closes
            highs = [float(v) for v in frame["High"]]
            lows = [float(v) for v in frame["Low"]]
            adx = feat.calc_adx(closes, highs, lows)
            distribution = feat.calc_distribution_days(closes, volumes)
            from tradingagents.quant.params import active as active_params

            threshold = active_params().distribution_day_threshold
            regime = policy.classify_market_regime(tf, adx, distribution, threshold)
            liquidity = feat.calc_liquidity(volumes)
            vni_regime_line = (
                f"Chế độ thị trường VNINDEX: {_REGIME_VI.get(regime, regime)}; {distribution} ngày phân phối "
                f"trong 25 phiên (≥{threshold} là risk-off); "
                + rfmt.adx_direction_line(adx.adx, adx.di_plus, adx.di_minus, adx.trending, available=adx.available)
                + f"; thanh khoản phiên gần nhất {rfmt.fmt_number(liquidity.liquidity_ratio_pct, 1)}% TB20"
            )
    if vni_closes:
        lines += ["", vni_regime_line]
    perf = None if vn_rules.is_index(symbol) else sector.sector_performance(symbol, as_of_date, vni_closes)
    lines.append("")
    if perf is not None:
        vs = (
            f", so với VNINDEX {rfmt.fmt_signed_pct(perf.vs_vnindex_1m)}" if perf.vs_vnindex_1m is not None else ""
        )
        lines.append(
            f"Ngành của {symbol} - {perf.label} ({perf.sample} mã đại diện): 1 tháng {rfmt.fmt_signed_pct(perf.trend_1m)}, "
            f"3 tháng {rfmt.fmt_signed_pct(perf.trend_3m)}{vs}; trạng thái {perf.momentum} "
            "(hot >5%/tháng, warm >1%, cold >-3%, dump còn lại)."
        )
    else:
        lines.append(f"Ngành của {symbol}: chưa có trong bản đồ ngành hoặc thiếu dữ liệu mã cùng ngành.")
    return "\n".join(lines)
