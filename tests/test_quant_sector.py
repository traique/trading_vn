"""Test tính toàn vẹn của SECTOR_MAP (port từ lananh-main test_sector_map.py).

SECTOR_MAP vừa là bản đồ ngành, vừa là nguồn sinh ALL_KNOWN_SYMBOLS. Sai sót
ở đây lan sang các tầng dùng nó, nên bắt bằng test thay vì bằng mắt.

Không port: test_no_symbol_collides_with_excluded_words và
test_ambiguous_known_symbols_exist_in_map (phụ thuộc danh sách loại trừ /
_AMBIGUOUS_KNOWN của stock/analysis.py - tầng nhận diện mã không được port).
"""
import re

from tradingagents.quant import fundamental_profiles, sector as stock_sector

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,4}$")


def test_every_sector_has_label_and_symbols():
    for key, meta in stock_sector.SECTOR_MAP.items():
        assert meta.get("label"), f"ngành {key} thiếu label"
        assert meta.get("symbols"), f"ngành {key} không có mã nào"


def test_symbol_format_is_valid():
    for key, meta in stock_sector.SECTOR_MAP.items():
        for sym in meta["symbols"]:
            assert _SYMBOL_RE.match(sym), f"mã {sym!r} trong ngành {key} sai định dạng"


def test_no_duplicate_symbol_within_a_sector():
    """Trùng trong CÙNG một ngành là lỗi gõ; trùng giữa các ngành thì hợp lệ
    (vd REE ở cả industrial lẫn utilities)."""
    for key, meta in stock_sector.SECTOR_MAP.items():
        symbols = meta["symbols"]
        assert len(symbols) == len(set(symbols)), f"ngành {key} có mã trùng lặp"


def test_gvr_is_covered():
    """Ca gây lỗi 30/07/2026: GVR không có trong bản đồ nên phần bình luận
    ngành trả rỗng và mã bị xếp vào nhóm chưa xác minh."""
    assert "GVR" in stock_sector.ALL_KNOWN_SYMBOLS
    assert "rubber" in stock_sector.get_symbol_sectors("GVR")
    assert stock_sector.get_primary_sector_label("GVR") == "Cao su & Săm lốp"


def test_newly_added_sectors_are_reachable():
    for key in ("rubber", "chemicals", "insurance", "aviation", "textile", "pharma", "materials", "seafood"):
        assert key in stock_sector.SECTOR_MAP


def test_unknown_symbol_falls_back_to_khac():
    assert stock_sector.get_symbol_sectors("ZZZZ") == []
    assert stock_sector.get_primary_sector_label("ZZZZ") == "Khác"


def test_lookup_is_case_insensitive():
    assert stock_sector.get_symbol_sectors("gvr") == stock_sector.get_symbol_sectors("GVR")
    assert stock_sector.get_symbol_sectors("  gvr  ") == stock_sector.get_symbol_sectors("GVR")


# Port từ test_stock_broker_upgrade.py::test_sector_profiles
def test_sector_profiles():
    assert fundamental_profiles.get_profile("VCB").benchmark_metric == "pb"
    assert fundamental_profiles.get_profile("SSI").benchmark_metric == "pb"
    assert fundamental_profiles.get_profile("VHM").benchmark_metric == "pb"
    assert fundamental_profiles.get_profile("FPT").benchmark_metric == "pe"
