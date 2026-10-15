"""Bản đồ ngành cổ phiếu Việt Nam và hiệu suất ngành so với VNINDEX (port từ lananh-main).

Hiệu suất ngành được tính trên dữ liệu giá point-in-time (``load_ohlcv``), nên
một lượt phân tích ngày quá khứ chỉ thấy các phiên đã đóng cửa tới ngày đó.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pandas as pd

from tradingagents.quant import features as feat

logger = logging.getLogger(__name__)

SECTOR_SAMPLE_SIZE = 8

# SECTOR_MAP có HAI vai trò:
#   1. Dữ liệu cho phân tích luân chuyển ngành (sector_performance).
#   2. Danh sách mã cùng ngành để so định giá (P/E hoặc P/B trung bình ngành).
#
# LƯU Ý khi thêm mã:
#   - SECTOR_SAMPLE_SIZE chỉ lấy 8 mã ĐẦU của mỗi ngành để tính hiệu suất và
#     định giá ngành, nên hãy xếp các mã vốn hoá lớn / tiêu biểu lên trước.
#   - Một mã có thể thuộc nhiều ngành (vd REE ở cả electrical lẫn utilities).
#
# ĐỘ MỊN CỦA NGÀNH là vấn đề đúng/sai, không phải thẩm mỹ: hiệu suất ngành
# được đưa thẳng vào báo cáo như một luận cứ ("ngành X giảm 11,51%/1 tháng").
# CII (hạ tầng giao thông BOT) và GEX (thiết bị điện) từng nằm chung một ngành
# gộp "Khu công nghiệp & Xây dựng" nên hai báo cáo khác nhau nhận CÙNG một con
# số ngành - một luận cứ sai cho cả hai mã. Vì vậy nhóm này được tách thành
# industrial_park / construction / electrical.
SECTOR_MAP: dict[str, dict] = {
    "banking":    {"label": "Ngân hàng",                 "symbols": ["VCB", "BID", "CTG", "TCB", "MBB", "ACB", "VPB", "HDB", "STB", "EIB", "TPB", "SHB", "VIB", "LPB", "SSB", "MSB", "OCB", "NAB", "BAB", "ABB"]},
    "steel":      {"label": "Thép",                       "symbols": ["HPG", "HSG", "NKG", "TLH", "SMC", "VGS", "TVN"]},
    "realestate": {"label": "Bất động sản",               "symbols": ["VIC", "VHM", "NVL", "KDH", "DXG", "PDR", "NLG", "DIG", "VRE", "KBC", "BCM", "HDC", "IJC", "SCR", "TCH", "AGG", "NTL", "QCG", "LDG", "HQC", "ITA", "TDC"]},
    "oilgas":     {"label": "Dầu khí",                    "symbols": ["GAS", "PLX", "PVD", "PVT", "PVS", "BSR", "OIL", "PLC", "PVC", "PVB", "PGD", "CNG"]},
    "technology": {"label": "Công nghệ",                  "symbols": ["FPT", "CMG", "VGI", "CTR", "ELC", "ITD", "SGT", "FOX"]},
    "securities": {"label": "Chứng khoán",                "symbols": ["SSI", "VCI", "HCM", "VND", "VIX", "SHS", "MBS", "BVS", "FTS", "BSI", "CTS", "AGR", "VDS", "ORS", "DSC"]},
    "retail":     {"label": "Bán lẻ",                     "symbols": ["MWG", "FRT", "PNJ", "DGW", "PET", "HAX"]},
    "food":       {"label": "Thực phẩm & Đồ uống",        "symbols": ["VNM", "SAB", "MSN", "DBC", "HAG", "QNS", "MCH", "KDC", "SBT", "BAF", "HNG", "LSS"]},
    "seafood":    {"label": "Thuỷ sản",                   "symbols": ["VHC", "ANV", "FMC", "IDI", "MPC", "ASM", "ACL", "CMX"]},
    "rubber":     {"label": "Cao su & Săm lốp",           "symbols": ["GVR", "PHR", "DPR", "DRC", "CSM", "DRI", "TRC", "RTB"]},
    "chemicals":  {"label": "Hoá chất & Phân bón",        "symbols": ["DGC", "DPM", "DCM", "CSV", "LAS", "BFC", "DDV"]},
    "insurance":  {"label": "Bảo hiểm",                   "symbols": ["BVH", "BMI", "PVI", "MIG", "PTI", "BIC", "ABI"]},
    "aviation":   {"label": "Hàng không",                 "symbols": ["HVN", "VJC", "ACV", "SCS", "SAS", "AST", "NCT"]},
    "textile":    {"label": "Dệt may",                    "symbols": ["VGT", "TNG", "MSH", "TCM", "STK", "GIL"]},
    "pharma":     {"label": "Dược phẩm & Y tế",           "symbols": ["DHG", "IMP", "DBD", "DVN", "DHT"]},
    "materials":  {"label": "Vật liệu xây dựng",          "symbols": ["VGC", "HT1", "BMP", "NTP", "VCS", "PTB", "BCC", "CVT", "KSB", "DHA"]},
    "industrial_park": {"label": "Khu công nghiệp",       "symbols": ["KBC", "BCM", "IDC", "SIP"]},
    "construction": {"label": "Xây dựng & Hạ tầng",       "symbols": ["CTD", "VCG", "HHV", "CII", "LCG", "FCN", "TCD"]},
    "electrical": {"label": "Thiết bị điện & Công nghiệp", "symbols": ["GEX", "REE", "PC1"]},
    "utilities":  {"label": "Điện & Tiện ích",            "symbols": ["POW", "REE", "GAS", "PLC", "NT2", "PC1", "GEG", "VSH", "QTP", "HDG"]},
    "logistics":  {"label": "Vận tải & Logistics",        "symbols": ["GMD", "PVT", "ACV", "VJC", "VTP", "HAH", "VSC", "VOS", "TMS", "PHP"]},
}

ALL_KNOWN_SYMBOLS: set[str] = {s for meta in SECTOR_MAP.values() for s in meta["symbols"]}


def get_symbol_sectors(symbol: str) -> list[str]:
    s = symbol.strip().upper()
    return [key for key, meta in SECTOR_MAP.items() if s in meta["symbols"]]


def get_primary_sector_label(symbol: str) -> str:
    keys = get_symbol_sectors(symbol)
    return SECTOR_MAP[keys[0]]["label"] if keys else "Khác"


def sector_peers(symbol: str, limit: int = SECTOR_SAMPLE_SIZE) -> list[str]:
    """Tối đa ``limit`` mã cùng ngành chính (không gồm chính mã đó)."""
    keys = get_symbol_sectors(symbol)
    if not keys:
        return []
    s = symbol.strip().upper()
    return [p for p in SECTOR_MAP[keys[0]]["symbols"] if p != s][:limit]


@dataclass
class SectorPerformance:
    key: str
    label: str
    trend_1m: float
    trend_3m: float
    vs_vnindex_1m: float | None
    momentum: str  # hot / warm / cold / dump
    sample: int


def _momentum(trend_1m: float) -> str:
    if trend_1m > 5:
        return "hot"
    if trend_1m > 1:
        return "warm"
    if trend_1m > -3:
        return "cold"
    return "dump"


# A peer whose last bar is more than this many sessions behind the freshest
# peer (suspended, vendor not updating - load_ohlcv accepts up to 12 calendar
# days) would compare a different date range with VNINDEX: left out.
MAX_PEER_LAG_SESSIONS = 1
_BARS_1M = 23  # 22 sessions of change
_BARS_3M = 66  # 65 sessions of change


def _close_on_or_before(dates: list[str], closes: list[float], day: str) -> float | None:
    from bisect import bisect_right

    i = bisect_right(dates, day)
    return closes[i - 1] if i > 0 else None


def _calendar(series: list[tuple[list[str], list[float]]]) -> list[str]:
    """Session calendar: every date any peer traded on, ascending."""
    return sorted({day for dates, _ in series for day in dates})


def _aligned_trends(
    series: list[tuple[list[str], list[float]]], bars: int
) -> list[float]:
    """Each peer's move from the session ``bars - 1`` sessions back on the
    peers' common calendar to its last close. A peer with no bar on
    or before that session (listed later, too short) is left out, so a short
    history does not pass a shorter move off as the full horizon."""
    reference = _calendar(series)
    if len(reference) < bars:
        return []
    start_day = reference[-bars]
    out = []
    for dates, closes in series:
        start = _close_on_or_before(dates, closes, start_day)
        if start and start > 0:
            out.append(feat.trend_pct([start, closes[-1]]))
    return out


def sector_performance(
    symbol: str, as_of_date: str, vnindex_closes: list[float] | None = None
) -> SectorPerformance | None:
    """Hiệu suất 1 tháng (22 phiên) / 3 tháng (65 phiên) bình quân của ngành chính.

    Mã nào không lấy được giá, hoặc phiên cuối trễ hơn mã mới nhất quá
    MAX_PEER_LAG_SESSIONS phiên (tạm ngừng giao dịch), thì bỏ qua; dưới 2 mã có
    dữ liệu thì trả None. Điểm đầu của mỗi mã được căn theo NGÀY trên lịch phiên
    chung của các mã, không theo vị trí mảng. ``vs_vnindex_1m`` là None khi không
    có chuỗi VNINDEX - không bịa so sánh.
    """
    from tradingagents.dataflows.ohlcv import load_ohlcv

    keys = get_symbol_sectors(symbol)
    if not keys:
        return None
    key = keys[0]
    members = SECTOR_MAP[key]["symbols"][:SECTOR_SAMPLE_SIZE]

    def bars(code: str) -> tuple[list[str], list[float]] | None:
        try:
            frame = load_ohlcv(code, as_of_date)
            closes = [float(c) for c in frame["Close"].tolist()]
            dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in frame["Date"].tolist()]
        except Exception:  # noqa: BLE001 - one missing peer must not sink the sector
            return None
        return (dates, closes) if len(dates) == len(closes) else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        series = [b for b in pool.map(bars, members) if b and len(b[1]) >= _BARS_1M]
    if not series:
        return None
    reference = _calendar(series)
    series = [
        (d, c) for d, c in series
        if sum(1 for day in reference if day > d[-1]) <= MAX_PEER_LAG_SESSIONS
    ]
    if len(series) < 2:
        return None
    moves_1m = _aligned_trends(series, _BARS_1M)
    if len(moves_1m) < 2:
        return None
    trend_1m = sum(moves_1m) / len(moves_1m)
    moves_3m = _aligned_trends(series, _BARS_3M)
    # No peer covers 3 months (sector of new listings): the longest window on
    # the common calendar, the same for every peer, rather than mixed horizons.
    if not moves_3m:
        moves_3m = _aligned_trends(series, min(_BARS_3M, len(_calendar(series)))) or moves_1m
    trend_3m = sum(moves_3m) / len(moves_3m)
    vs = None
    if vnindex_closes and len(vnindex_closes) >= _BARS_1M:
        vs = round(trend_1m - feat.trend_pct(vnindex_closes[-_BARS_1M:]), 2)
    return SectorPerformance(
        key=key,
        label=SECTOR_MAP[key]["label"],
        trend_1m=round(trend_1m, 2),
        trend_3m=round(trend_3m, 2),
        vs_vnindex_1m=vs,
        momentum=_momentum(trend_1m),
        sample=len(moves_1m),
    )
