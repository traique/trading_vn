"""Hồ sơ định giá theo ngành: ngân hàng/chứng khoán/bảo hiểm/BĐS so theo P/B, còn lại P/E."""

from dataclasses import dataclass

from tradingagents.quant import sector


@dataclass(frozen=True)
class FundamentalProfile:
    key: str
    label: str
    benchmark_metric: str
    priority_metrics: tuple[str, ...]
    suppress_metrics: tuple[str, ...] = ()
    note: str = ""


PROFILES = {
    "banking": FundamentalProfile("banking", "Ngân hàng", "pb", ("pb", "roe", "eps", "profit_growth"), ("current_ratio", "debt_equity"), "Ưu tiên P/B và ROE; D/E/current ratio doanh nghiệp thường không phù hợp."),
    "securities": FundamentalProfile("securities", "Chứng khoán", "pb", ("pb", "roe", "profit_growth"), ("current_ratio",), "Ưu tiên P/B, ROE và độ nhạy lợi nhuận theo thanh khoản thị trường."),
    "insurance": FundamentalProfile("insurance", "Bảo hiểm", "pb", ("pb", "roe", "profit_growth"), ("current_ratio",), "Ưu tiên P/B và ROE; cần đối chiếu dự phòng khi có dữ liệu."),
    "realestate": FundamentalProfile("realestate", "Bất động sản", "pb", ("pb", "debt_equity", "current_ratio", "profit_growth"), (), "Ưu tiên tài sản ròng, đòn bẩy, thanh khoản và tiến độ dự án."),
    "utilities": FundamentalProfile("utilities", "Điện & Tiện ích", "pe", ("pe", "dividend_yield", "debt_equity", "roe"), (), "Ưu tiên dòng tiền/cổ tức và đòn bẩy hạ tầng."),
    "oilgas": FundamentalProfile("oilgas", "Dầu khí", "pe", ("pe", "roe", "profit_growth", "debt_equity"), (), "Đọc định giá cùng chu kỳ giá hàng hóa."),
    "default": FundamentalProfile("default", "Doanh nghiệp", "pe", ("pe", "pb", "roe", "profit_growth", "debt_equity", "current_ratio")),
}

# Loại hình doanh nghiệp theo VCI (details.comTypeCode): NH ngân hàng, CK công ty
# chứng khoán, BH bảo hiểm, CT doanh nghiệp thường. Ưu tiên hơn bản đồ ngành vì
# phủ mọi mã niêm yết.
_COM_TYPE_PROFILE = {"NH": "banking", "CK": "securities", "BH": "insurance"}


def get_profile(symbol: str, com_type: str | None = None) -> FundamentalProfile:
    key = _COM_TYPE_PROFILE.get((com_type or "").upper())
    if key:
        return PROFILES[key]
    for key in sector.get_symbol_sectors(symbol):
        if key in PROFILES:
            return PROFILES[key]
    return PROFILES["default"]
