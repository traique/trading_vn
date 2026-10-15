"""Regression test cho các lỗi của đợt review hệ thống phân tích cổ phiếu
(port từ lananh-main test_stock_review_fixes.py).

Chỉ giữ các test gọi trực tiếp policy/features/price_adjust/report_format.
Các test đi qua analysis/backtest/corporate_actions/providers/trend_model/
vci_direct/personas/portfolio/channel service không được port vì các module
đó không có trong tradingagents.quant.
"""

import pytest

from tests.test_quant_policy import _closes_uptrend, _make_inputs
from tradingagents.quant import features as feat, policy as pol, price_adjust, report_format as rf
from tradingagents.quant.validation import DataQuality

# ─── 19. Dữ liệu cũ chặn BUY ─────────────────────────────────────────────────


def test_du_lieu_stale_chan_buy(monkeypatch):
    monkeypatch.setattr(pol, "_compute_confidence", lambda *a, **k: 0.9)
    inputs = _make_inputs(_closes_uptrend(90), relative_strength=8.0)
    p = inputs.price
    inputs.support_resistance = feat.SupportResistance(
        round(p * 0.97),
        round(p * 1.12),
        0,
        0,
        resistance_prior=round(p * 1.12),
        support_prior=round(p * 0.97),
    )
    assert pol.evaluate_policy(inputs).action == "BUY"
    inputs.quality = DataQuality(
        status="degraded", reasons=["dữ liệu cũ"], bars_available=90, is_stale=True
    )
    decision = pol.evaluate_policy(inputs)
    assert decision.action != "BUY"
    assert decision.trade_plan is None
    assert any("cũ" in r for r in decision.reasons)


# ─── 4/5. Gate D: kháng cự thật, R:R tại giá khớp xấu nhất ──────────────────


def _forced_buy_inputs(monkeypatch, resistance_factor):
    monkeypatch.setattr(pol, "_compute_confidence", lambda *a, **k: 0.9)
    inputs = _make_inputs(_closes_uptrend(90), relative_strength=8.0)
    p = inputs.price
    resistance = round(p * resistance_factor) if resistance_factor else None
    inputs.support_resistance = feat.SupportResistance(
        round(p * 0.97),
        resistance,
        0,
        0,
        resistance_prior=resistance,
        support_prior=round(p * 0.97),
    )
    return inputs


def test_khong_co_khang_cu_that_thi_target_uoc_luong_khong_mo_buy(monkeypatch):
    decision = pol.evaluate_policy(_forced_buy_inputs(monkeypatch, None))
    assert decision.action == "WATCH"
    assert decision.trade_plan is None
    assert any("không có kháng cự thật" in r for r in decision.reasons)


def test_khang_cu_gan_hon_1r_duoc_ton_trong(monkeypatch):
    inputs = _forced_buy_inputs(monkeypatch, 1.02)
    decision = pol.evaluate_policy(inputs)
    # T1 is the highest valid price AT OR BELOW the resistance (it used to
    # round to the nearest tick, which could land one tick above the level).
    assert decision.target_price == pol._target_at_or_below(round(inputs.price * 1.02))
    assert decision.target_price <= round(inputs.price * 1.02)
    assert decision.action == "WATCH"
    assert decision.rr_ratio < pol.MIN_RR_RATIO


def test_buy_rr_va_ty_trong_tinh_tai_can_tren_vung_mua(monkeypatch):
    decision = pol.evaluate_policy(_forced_buy_inputs(monkeypatch, 1.12))
    assert decision.action == "BUY"
    plan = decision.trade_plan
    assert plan is not None
    assert plan.entry_low > plan.stop
    worst_rr = round((plan.target1 - plan.entry_high) / (plan.entry_high - plan.stop), 2)
    assert decision.rr_ratio == worst_rr >= pol.MIN_RR_RATIO
    loss_pct_nav = plan.position_size_pct * (plan.entry_high - plan.stop) / plan.entry_high
    assert loss_pct_nav <= pol.RISK_PER_TRADE_PCT + 1e-6


def test_entry_zone_luon_cao_hon_stop_khi_stop_sat_gia():
    zone = pol.entry_zone(50_000, 49_500, "HOSE")
    assert zone is not None
    low, high = zone
    assert low > 49_500 and high >= 50_000


def test_target_uoc_luong_khong_lot_ra_decision(monkeypatch):
    inputs = _forced_buy_inputs(monkeypatch, None)
    inputs.is_holding = True
    decision = pol.evaluate_policy(inputs)
    assert decision.action == "HOLD"
    assert (
        decision.target_price is None and decision.rr_ratio is None and decision.trade_plan is None
    )


# ─── 3. Gap cổ phiếu thưởng chưa điều chỉnh ─────────────────────────────────


def test_policy_gap_chua_dieu_chinh_khong_ra_tin_hieu_dinh_huong():
    inputs = _make_inputs(_closes_uptrend(90), relative_strength=8.0)
    inputs.unadjusted_gap = True
    inputs.is_holding = True
    decision = pol.evaluate_policy(inputs)
    assert decision.action == "HOLD"
    assert decision.setup_type == "none"
    assert decision.stop_price is None and decision.target_price is None
    inputs.is_holding = False
    assert pol.evaluate_policy(inputs).action == "NO_TRADE"


def test_last_gap_in_window_chi_xet_cua_so_policy():
    closes = [20_000.0] * 100 + [16_000.0] * 10
    gaps = price_adjust.detect_price_gaps(closes)
    assert price_adjust.last_gap_in_window(gaps, len(closes)).index == 100
    old = [20_000.0] * 10 + [16_000.0] * 100
    assert price_adjust.last_gap_in_window(price_adjust.detect_price_gaps(old), len(old)) is None


# ─── 8. VNINDEX căn theo ngày ────────────────────────────────────────────────


def test_benchmark_end_indices_theo_ngay_khong_look_ahead():
    vn_dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    sym_dates = ["2026-01-05", "2026-01-08", "2026-01-09"]  # mã nghỉ 2 phiên
    assert feat.benchmark_end_indices(sym_dates, vn_dates, 3, 5) == [1, 4, 5]
    assert feat.benchmark_end_indices(["0", "1"], ["0", "1"], 2, 2) == [
        1,
        2,
    ]  # không có ngày -> tương thích cũ


# ─── 10. Disclaimer không xoá nội dung phân tích ────────────────────────────


def test_cum_tu_long_khong_lam_mat_noi_dung_phan_tich():
    text = (
        "Kết luận: MUA FPT, anh cân nhắc kỹ trước khi vào lệnh.\n\n"
        "Kế hoạch: vùng mua 100.000, stop 95.000.\n\n"
        "Đây là khuyến nghị đầu tư mạnh nhất tuần: tỷ trọng 20% NAV."
    )
    out = rf.ensure_disclaimer(rf.clean_analysis_output(text))
    assert "Kế hoạch: vùng mua 100.000" in out
    assert "tỷ trọng 20% NAV" in out
    assert out.endswith(rf.STANDARD_DISCLAIMER) and out.count(rf.STANDARD_DISCLAIMER) == 1


# ─── 13. Hợp đồng policy được kiểm trong code ───────────────────────────────


def test_find_action_conflicts_bo_qua_cho_phu_dinh():
    assert rf.find_action_conflicts("Kết luận: MUA ngay", {"WATCH"}) == ["BUY:MUA"]
    assert rf.find_action_conflicts("Hiện KHÔNG MUA đuổi, chưa nên mua", {"WATCH"}) == []
    assert rf.find_action_conflicts("Tín hiệu THEO DÕI", {"WATCH"}) == []
    assert rf.find_plan_language("Vùng mua 25.000, stop 24.000")


def test_find_unknown_prices():
    prompt = "Giá 25.000 VND, kháng cự 27.500 VND, KL 1.234.567"
    assert rf.find_unknown_prices("Kháng cự 27.500, KL 1.234.567", prompt) == []
    assert rf.find_unknown_prices("Target 30.000", prompt) == [30000]


def test_cau_dieu_kien_trong_ke_hoach_khong_bi_coi_la_doi_action():
    assert (
        rf.find_action_conflicts("Nếu thủng 48.500 thì nên bán để quản trị rủi ro.", {"BUY"}) == []
    )
    assert rf.find_action_conflicts("Nếu đóng cửa dưới stop thì nên cắt lỗ.", {"BUY"}) == []
    assert rf.find_action_conflicts("Giá đã lên. Nên bán ngay.", {"BUY"}) == ["SELL:Nên bán"]
    # Bản tham khảo: câu điều kiện có khuyến nghị vẫn bị cấm.
    assert rf.find_plan_language("Nếu giá về 25.000 thì nên mua")
    assert rf.find_plan_language("Tỷ trọng doanh thu mảng phần mềm tăng") == []


def test_find_unknown_prices_bo_qua_diem_vnindex():
    prompt = "Giá đóng cửa 56.300, hỗ trợ 55.000"
    text = (
        "VNINDEX 1.719,42 điểm, VN-Index quanh 1.700, mốc 1.750 điểm. "
        "VNINDEX giảm 2%, FPT 58.000"
    )
    assert rf.find_unknown_prices(text, prompt) == [58000]
    # Giá cổ phiếu sàn HNX vẫn là giá, không phải điểm chỉ số.
    assert rf.find_unknown_prices("Mã sàn HNX giá 27.500", prompt) == [27500]


def test_relative_strength_can_vnindex_theo_ngay():
    from tradingagents.quant import engine

    dates = ["2026-01-02", "2026-01-05", "2026-01-07"]          # FPT nghỉ 06/01
    vni_dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    closes, vni = engine._align_to_benchmark([10.0, 11.0, 12.0], dates, [100.0, 101.0, 150.0, 102.0], vni_dates)
    assert closes == [10.0, 11.0, 12.0]
    assert vni == [100.0, 101.0, 102.0]
    # Bar cũ hơn chuỗi VNINDEX bị bỏ, không ghép với VNINDEX tương lai.
    closes, vni = engine._align_to_benchmark([9.0, 10.0], ["2025-12-31", "2026-01-02"], [100.0], ["2026-01-02"])
    assert (closes, vni) == ([10.0], [100.0])


# ─── Output Guard: the review's cases ────────────────────────────────────────

_BUY = frozenset({"BUY"})
_NO_TRADE = frozenset({"NO_TRADE", "WATCH"})


@pytest.mark.parametrize("text", [
    "Nếu thủng stop 50.800 thì BÁN cắt lỗ",
    "Hàng mua về T+2 mới được BÁN",
    "Bên BÁN đang yếu dần",
    "Khối ngoại bán ra 300 tỷ trong tuần",
])
def test_scenarios_rules_and_market_sides_are_not_contradictions(text):
    assert rf.find_action_conflicts(text, _BUY) == []


@pytest.mark.parametrize("text", [
    "Áp lực BÁN lớn", "Tín hiệu MUA chưa xác nhận", "Khối ngoại mua vào 200 tỷ", "không nên mua đuổi",
])
def test_market_descriptions_do_not_contradict_no_trade(text):
    assert rf.find_action_conflicts(text, _NO_TRADE) == []


@pytest.mark.parametrize("text", [
    "Mua vào quanh 56.000", "Khuyến nghị: Mua", "Giải ngân 30% NAV", "tăng tỷ trọng", "Overweight",
    "Rủi ro không lớn, MUA ngay",
])
def test_plain_calls_against_no_trade_are_caught(text):
    assert rf.find_action_conflicts(text, _NO_TRADE)


def test_prices_at_the_end_of_a_sentence_and_non_price_numbers():
    assert rf.price_numbers("Mua quanh 55.500.") == {55500}
    assert rf.price_numbers("KL 2.345.600 cp, EPS 5.000đ, vốn hóa 12.345 tỷ, NAV 1.000.000.000") == set()


def test_only_the_models_own_task_done_line_is_removed():
    kept = "Ban lãnh đạo đặt nhiệm vụ hoàn thành kế hoạch lợi nhuận 9.000 tỷ."
    assert kept in rf.clean_analysis_output(kept + "\n\nEm đã hoàn thành nhiệm vụ.")
    assert "Em đã hoàn thành" not in rf.clean_analysis_output(kept + "\n\nEm đã hoàn thành nhiệm vụ.")


def test_watch_and_sell_carry_no_stop_or_target_lines():
    from tradingagents.agents.schemas import plan_lines

    watch = plan_lines({"action": "WATCH", "stop_price": 31_100, "target_price": 32_950, "rr_ratio": 0.23})
    assert "**Stop Loss**: not provided" in watch and "**Risk/Reward**: not provided" in watch
    sell = plan_lines({"action": "SELL", "stop_price": 60_000, "target_price": 52_000})
    assert "**Stop Loss**: not provided" in sell and any(line.startswith("**Invalidation Level**") for line in sell)
