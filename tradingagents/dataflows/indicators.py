"""Technical indicators computed locally (stockstats) on the point-in-time loader."""

from __future__ import annotations

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.ohlcv import load_ohlcv
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.market import vn_rules

INDICATOR_DESCRIPTIONS = {
    "close_50_sma": (
        "SMA 50: xu hướng trung hạn, đóng vai trò hỗ trợ/kháng cự động. "
        "Trễ so với giá; kết hợp chỉ báo nhanh hơn để có tín hiệu kịp thời."
    ),
    "close_200_sma": (
        "SMA 200: thước đo xu hướng dài hạn, dùng xác nhận xu hướng chung và golden/death cross. "
        "Phản ứng chậm, hợp cho xác nhận chiến lược hơn là điểm vào lệnh."
    ),
    "close_10_ema": (
        "EMA 10: trung bình ngắn hạn nhạy, bắt nhịp thay đổi động lượng. "
        "Dễ nhiễu khi thị trường đi ngang; nên lọc bằng đường dài hơn."
    ),
    "macd": (
        "MACD: động lượng từ chênh lệch các EMA. Theo dõi giao cắt và phân kỳ để nhận diện đổi xu hướng."
    ),
    "macds": "Đường tín hiệu MACD (EMA của MACD): giao cắt với MACD tạo tín hiệu.",
    "macdh": (
        "MACD Histogram: khoảng cách MACD - tín hiệu, cho thấy sức mạnh động lượng và phân kỳ sớm."
    ),
    "rsi": (
        "RSI: đo động lượng, ngưỡng 70/30 cho quá mua/quá bán. "
        "Trong xu hướng mạnh RSI có thể neo ở vùng cực trị lâu; luôn đối chiếu xu hướng."
    ),
    "boll": "Bollinger giữa: SMA 20 làm chuẩn cho biến động giá.",
    "boll_ub": "Bollinger trên: thường cách đường giữa 2 độ lệch chuẩn - vùng quá mua/breakout.",
    "boll_lb": "Bollinger dưới: thường cách đường giữa 2 độ lệch chuẩn - vùng quá bán.",
    "atr": (
        "ATR: biên độ dao động thực trung bình, dùng đặt stop và định cỡ vị thế theo biến động. "
        "Lưu ý biên độ trần/sàn trong ngày giới hạn ATR (HOSE ±7%)."
    ),
    "vwma": "VWMA: trung bình giá có trọng số khối lượng, xác nhận xu hướng bằng dòng tiền.",
    "mfi": (
        "MFI: chỉ số dòng tiền dùng cả giá và khối lượng, >80 quá mua, <20 quá bán; "
        "phân kỳ giá - MFI có thể báo đảo chiều."
    ),
}


def _indicator_series(symbol: str, indicator: str, as_of_date: str) -> pd.DataFrame:
    data = load_ohlcv(symbol, as_of_date)
    df = wrap(data.copy())
    df[indicator]  # triggers the stockstats calculation
    out = pd.DataFrame({"Date": pd.to_datetime(data["Date"]).to_numpy(), "value": df[indicator].to_numpy()})
    return out


def get_indicator_window(symbol: str, indicator: str, as_of_date: str, look_back_days: int) -> str:
    """One indicator for each session in the ``look_back_days`` before ``as_of_date``."""
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS)}"
        )
    canonical = normalize_symbol(symbol)
    try:
        series = _indicator_series(canonical, indicator, as_of_date)
    except VendorError:
        raise
    except Exception as exc:  # noqa: BLE001 - a calculation failure is no data, not a crash
        raise NoMarketDataError(symbol, canonical, f"{indicator} could not be computed: {exc}") from exc
    start = pd.Timestamp(as_of_date) - pd.Timedelta(days=int(look_back_days))
    window = series[series["Date"] >= start].sort_values("Date", ascending=False)
    decimals = 2 if vn_rules.is_index(canonical) or indicator in {"rsi", "mfi", "macd", "macds", "macdh"} else 0
    lines = []
    for _, row in window.iterrows():
        value = row["value"]
        shown = "N/A (chưa đủ dữ liệu)" if pd.isna(value) else f"{value:.{decimals}f}"
        lines.append(f"{row['Date']:%Y-%m-%d}: {shown}")
    return (
        f"## {indicator} của {canonical} từ {start:%Y-%m-%d} đến {as_of_date} "
        f"(chỉ các phiên đã đóng cửa):\n\n" + "\n".join(lines) + "\n\n" + INDICATOR_DESCRIPTIONS[indicator]
    )
