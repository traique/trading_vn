"""The agents explain the system decision; code makes sure they kept it.

Port of lananh-main's ``_enforce_policy_contract`` checks to the graph: a text
that contradicts the system action is dropped for the system summary, a price
that is in no data the agents were given is flagged, and the final rating is
always the engine's action.
"""

import pytest

from tradingagents.agents import output_guard
from tradingagents.agents.quant_engine import create_quant_engine
from tradingagents.quant import engine, report_format as rfmt


def _decision(action="NO_TRADE", **extra):
    result = engine.QuantResult(
        symbol="FPT", as_of_date="2026-10-13", last_bar_date="2026-10-13", price=56_300.0,
        exchange="HOSE", action=action, confidence=0.79, setup_type="breakdown",
        market_regime="risk_off", risk_level="medium", data_quality="ok",
        reasons=["breakdown xuống dưới kênh Donchian 20 phiên"], stop_price=None,
        target_price=None, rr_ratio=None, invalidation_reason=None, **extra,
    )
    result.deterministic_summary = engine.render_summary(result)
    result.report = "Giá dùng để tính toán: 56.300 VND. Hỗ trợ gần nhất 55.910 VND (-0,69%)."
    return result.to_state()


def _state(action="NO_TRADE", **texts):
    decision = _decision(action)
    return {
        "quant_decision": decision,
        "quant_report": decision["report"],
        "market_report": "Đóng cửa 56.300 VND ngày 13/10/2026.",
        "investment_plan": texts.get("plan", "**Recommendation**: NO_TRADE\n\nĐứng ngoài quan sát."),
        "trader_investment_plan": texts.get("trader", "**Action**: NO_TRADE\n\nChưa có kế hoạch."),
        "final_trade_decision": texts.get("final", "**Rating**: NO_TRADE\n\nChờ tín hiệu rõ hơn."),
    }


@pytest.mark.unit
def test_a_decision_that_keeps_the_action_passes_untouched():
    out = output_guard.create_output_guard()(_state())
    assert out["guard_notes"] == ""
    assert "Chờ tín hiệu rõ hơn." in out["final_trade_decision"]
    assert out["final_rating"] == "NO_TRADE"


@pytest.mark.unit
def test_a_contradicting_action_is_replaced_by_the_system_summary():
    state = _state(final="**Rating**: NO_TRADE\n\nKết luận: MUA ngay vì giá đã về vùng đáy.")
    out = output_guard.create_output_guard()(state)
    assert output_guard.CONFLICT_NOTE in out["final_trade_decision"]
    assert "MUA ngay" not in out["final_trade_decision"]
    assert "Tín hiệu hệ thống: ĐỨNG NGOÀI (NO_TRADE)" in out["final_trade_decision"]
    assert "Portfolio Manager" in out["guard_notes"]


@pytest.mark.unit
def test_advice_in_lower_case_is_caught_but_a_negated_or_conditional_one_is_not():
    guard = output_guard.guard_text
    state = _state()
    assert guard("Nhà đầu tư nên mua thêm.", state, label="x")[1] is not None
    assert guard("Chưa nên mua lúc này.", state, label="x")[1] is None
    assert guard("Nếu giá vượt kháng cự thì có thể canh mua.", state, label="x")[1] is None


@pytest.mark.unit
def test_watch_and_no_trade_are_compatible():
    text, note = output_guard.guard_text("Tín hiệu THEO DÕI, chưa vào lệnh.", _state("NO_TRADE"), label="x")
    assert note is None and text.startswith("Tín hiệu THEO DÕI")


@pytest.mark.unit
def test_a_price_from_nowhere_is_flagged_and_the_text_kept():
    state = _state(final="**Rating**: NO_TRADE\n\nĐứng ngoài; vùng 48.500 là hỗ trợ mạnh.")
    out = output_guard.create_output_guard()(state)
    assert out["final_trade_decision"].startswith(output_guard.UNKNOWN_PRICE_NOTE)
    assert "vùng 48.500 là hỗ trợ mạnh" in out["final_trade_decision"]
    assert "48.500" in out["guard_notes"]


@pytest.mark.unit
def test_prices_from_the_data_are_not_flagged():
    text = "Đứng ngoài; giá 56.300 VND, hỗ trợ 55.910 VND sát giá."
    assert output_guard.guard_text(text, _state(), label="x")[1] is None


@pytest.mark.unit
def test_the_system_written_lines_are_not_checked():
    decision = "**Rating**: BUY\n\n**Entry Zone**: 99.000 VND - 100.500 VND\n\nGiữ kỷ luật."
    state = _state("BUY", final=decision)
    assert output_guard.guard_text(decision, state, label="x")[1] is None


@pytest.mark.unit
def test_the_final_rating_is_the_system_action_and_one_disclaimer_ends_it():
    state = _state(final="**Rating**: NO_TRADE\n\nĐứng ngoài.\n\n" + rfmt.STANDARD_DISCLAIMER)
    out = output_guard.create_output_guard()(state)
    assert out["final_rating"] == "NO_TRADE"
    assert out["final_trade_decision"].count(rfmt.STANDARD_DISCLAIMER) == 1
    assert out["final_trade_decision"].endswith(rfmt.STANDARD_DISCLAIMER)


@pytest.mark.unit
def test_a_failed_engine_degrades_to_no_trade(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no prices")

    monkeypatch.setattr(engine, "analyze", boom)
    out = create_quant_engine()({"company_of_interest": "FPT", "trade_date": "2026-10-13"})
    assert out["quant_decision"]["action"] == "NO_TRADE"
    assert out["quant_report"] == ""
    assert "no prices" in out["quant_decision"]["reasons"][0]


@pytest.mark.unit
def test_the_engine_node_passes_the_book(monkeypatch):
    seen = {}

    def analyze(symbol, date, portfolio=None):
        seen.update(symbol=symbol, portfolio=portfolio)
        raise RuntimeError("stop")

    monkeypatch.setattr(engine, "analyze", analyze)
    create_quant_engine()({"company_of_interest": "FPT", "trade_date": "2026-10-13",
                           "portfolio": {"cash": 1e9, "positions": [{"ticker": "FPT.VN", "quantity": 500}]}})
    assert seen["portfolio"].position_in("FPT").quantity == 500
