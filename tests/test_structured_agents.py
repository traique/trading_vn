"""Tests for structured-output agents (Trader, Research Manager, Sentiment Analyst).

The Portfolio Manager has its own coverage in tests/test_memory_log.py
(which exercises the full memory-log → PM injection cycle).  This file
covers the parallel schemas, render functions, and graceful-fallback
behavior we added for the Trader, Research Manager, and Sentiment Analyst
so they share the same deterministic output shape.

The action and every price level are the quantitative engine's (state keys
``quant_decision`` / ``quant_report``); the schemas only carry the model's
explanation, and the renderers write the engine's action and numbers.
"""

import inspect
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    SentimentBand,
    SentimentReport,
    Stance,
    SystemAction,
    TraderProposal,
    _coerce_optional_float,
    render_research_plan,
    render_sentiment_report,
    render_trader_proposal,
)
from tradingagents.agents.trader.trader import create_trader

BUY_DECISION = {
    "action": "BUY", "exchange": "HOSE",
    "entry_low": 70500.0, "entry_high": 71200.0,
    "stop_price": 67000.0, "target_price": 78000.0, "target2": 82000.0,
    "rr_ratio": 2.1, "position_size_pct": 6.0, "suggested_shares": 800,
}
QUANT_REPORT = "ENGINE REPORT: FPT BUY, vùng mua 70.500 - 71.200 VND, stop 67.000 VND"

PLAN_FIELDS = ("Entry Zone", "Stop Loss", "Target 1", "Target 2", "Risk/Reward", "Position Sizing")

# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderTraderProposal:
    def test_minimal_required_fields(self):
        p = TraderProposal(reasoning="Balanced setup; no edge.", execution_notes="Không vào lệnh.")
        md = render_trader_proposal(p, {"action": "HOLD"})
        assert md.startswith("**Action**: HOLD")
        assert "**Reasoning**: Balanced setup; no edge." in md
        assert "**Execution Notes**: Không vào lệnh." in md
        # The trailing FINAL TRANSACTION PROPOSAL line is preserved for the
        # analyst stop-signal text and any external code that greps for it.
        assert md.rstrip().endswith("FINAL TRANSACTION PROPOSAL: **HOLD**")

    def test_plan_fields_come_from_the_engine(self):
        p = TraderProposal(reasoning="Strong technicals + fundamentals.", execution_notes="Chia 2 lệnh ATO.")
        md = render_trader_proposal(p, BUY_DECISION)
        assert "**Action**: BUY" in md
        assert "**Entry Zone**: 70.500 VND - 71.200 VND" in md
        assert "**Stop Loss**: 67.000 VND" in md
        assert "**Target 1**: 78.000 VND" in md
        assert "**Target 2**: 82.000 VND" in md
        assert "**Risk/Reward**: 2,10" in md
        assert "**Position Sizing**: 6,0% NAV (~800 cổ phiếu, lô 100)" in md
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in md

    def test_optional_fields_are_named_as_not_provided(self):
        """An omitted line reads as a field nobody asked for; the reader cannot
        tell it from a level the engine declined to set."""
        p = TraderProposal(reasoning="Guidance cut.", execution_notes="Bán hết sau T+2.")
        md = render_trader_proposal(p, {"action": "SELL"})
        for field in PLAN_FIELDS:
            assert f"**{field}**: not provided" in md
        assert "FINAL TRANSACTION PROPOSAL: **SELL**" in md

    def test_no_engine_decision_is_no_trade(self):
        md = render_trader_proposal(TraderProposal(reasoning="r", execution_notes="e"))
        assert md.startswith("**Action**: NO_TRADE")
        assert "FINAL TRANSACTION PROPOSAL: **NO_TRADE**" in md

    def test_the_model_cannot_supply_an_action_or_a_price(self):
        """The proposal carries only the explanation; an action or a level the
        model adds is not a field and cannot reach the rendered plan."""
        assert set(TraderProposal.model_fields) == {"reasoning", "execution_notes"}
        p = TraderProposal(reasoning="r", execution_notes="e", action="SELL", entry_price=1.0)
        md = render_trader_proposal(p, BUY_DECISION)
        assert "SELL" not in md


@pytest.mark.unit
class TestOptionalFloatCoercion:
    """A weak LLM may write "None"/"N/A" into an optional numeric field (#1058),
    a percentage where a price was asked (#1288), or a human-formatted price;
    the coercion keeps one bad value from failing a whole structured call."""

    def test_nullish_strings_coerce_to_none(self):
        for sentinel in ("None", "N/A", "null", "-", "", "TBD"):
            assert _coerce_optional_float(sentinel) is None

    def test_real_numeric_string_still_parses(self):
        assert _coerce_optional_float("189.5") == 189.5
        assert _coerce_optional_float(70500) == 70500

    def test_percentage_answer_to_a_price_field_becomes_none(self):
        # A percentage cannot be salvaged: 15% must not become a 15 VND stop.
        for pct in ("15%", " 7.5% ", "-10%"):
            assert _coerce_optional_float(pct) is None

    def test_human_formatted_price_is_reduced_to_its_number(self):
        assert _coerce_optional_float("$1,234.50") == 1234.50
        assert _coerce_optional_float("1,180") == 1180.0

    @pytest.mark.parametrize("written, expected", [
        ("70.500", 70500.0),           # dot as thousands separator
        ("1.234.500", 1234500.0),
        ("70,5 nghìn", 70500.0),       # decimal comma, thousand
        ("70,5k", 70500.0),
        ("1.234,5", 1234.5),           # Vietnamese decimal comma with thousands
        ("56.300 đ", 56300.0),
        ("70.500 VND", 70500.0),
        ("70.500 ₫", 70500.0),
    ])
    def test_vietnamese_notation_is_read(self, written, expected):
        assert _coerce_optional_float(written) == expected


@pytest.mark.unit
class TestRenderResearchPlan:
    def test_required_fields(self):
        p = ResearchPlan(
            stance=Stance.PARTLY_AGREE,
            rationale="Bull case carried; tailwinds intact.",
            strategic_actions="Theo dõi mốc 67.000 VND; KQKD quý 4.",
        )
        md = render_research_plan(p, "BUY")
        assert md.startswith("**Recommendation**: BUY (hành động của hệ thống định lượng)")
        assert "**Stance on the system action**: partly_agree" in md
        assert "**Rationale**: Bull case carried" in md
        assert "**Strategic Actions**: Theo dõi" in md

    def test_every_system_action_renders(self):
        for action in SystemAction:
            p = ResearchPlan(stance=Stance.AGREE, rationale="r", strategic_actions="s")
            md = render_research_plan(p, action.value)
            assert f"**Recommendation**: {action.value}" in md

    def test_default_action_is_no_trade(self):
        md = render_research_plan(ResearchPlan(stance=Stance.DISAGREE, rationale="r", strategic_actions="s"))
        assert md.startswith("**Recommendation**: NO_TRADE")

    def test_stance_is_one_of_three(self):
        assert {s.value for s in Stance} == {"agree", "partly_agree", "disagree"}
        with pytest.raises(ValidationError):
            ResearchPlan(stance="Overweight", rationale="r", strategic_actions="s")

    def test_portfolio_rating_is_the_system_action(self):
        assert PortfolioRating is SystemAction
        assert {a.value for a in SystemAction} == {"BUY", "HOLD", "WATCH", "SELL", "NO_TRADE"}


# ---------------------------------------------------------------------------
# Trader agent: structured happy path + fallback
# ---------------------------------------------------------------------------


def _make_trader_state(decision=None):
    return {
        "company_of_interest": "FPT",
        "investment_plan": "**Recommendation**: BUY\n**Rationale**: ...\n**Strategic Actions**: ...",
        "market_report": "Giá đóng cửa 70.900 VND; ATR 14 phiên 1.200; hỗ trợ 67.000, kháng cự 78.000.",
        "quant_decision": dict(BUY_DECISION if decision is None else decision),
        "quant_report": QUANT_REPORT,
    }


def _structured_trader_llm(captured: dict, proposal: TraderProposal | None = None):
    """Build a MagicMock LLM whose with_structured_output binding captures the
    prompt and returns a real TraderProposal so render_trader_proposal works.
    """
    if proposal is None:
        proposal = TraderProposal(reasoning="Strong setup.", execution_notes="Mua phiên ATO.")
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or proposal
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
def test_invoke_structured_falls_back_when_result_is_none():
    # A thinking model can answer in plain text, leaving the parser with None.
    # That must fall back to free text, not crash on render(None) (#1051).
    from tradingagents.agents.structured import invoke_structured_or_freetext

    structured = MagicMock()
    structured.invoke.return_value = None
    plain = MagicMock()
    plain.invoke.return_value = MagicMock(content="FREETEXT")

    out = invoke_structured_or_freetext(
        structured, plain, "prompt", render=lambda r: r.rating, agent_name="t"
    )
    assert out == "FREETEXT"
    plain.invoke.assert_called_once()


@pytest.mark.unit
class TestTraderAgent:
    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        proposal = TraderProposal(
            reasoning="Lợi nhuận quý tăng; khối ngoại mua ròng.",
            execution_notes="Chia lô 100, đặt lệnh phiên liên tục.",
        )
        llm = _structured_trader_llm(captured, proposal)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        plan = result["trader_investment_plan"]
        assert plan.startswith("**Action**: BUY")
        assert "**Entry Zone**: 70.500 VND - 71.200 VND" in plan
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in plan
        # The same rendered markdown is also added to messages for downstream agents.
        assert plan in result["messages"][0].content

    def test_prompt_includes_investment_plan_and_the_engine_decision(self):
        from tradingagents.agents import vn_context

        captured = {}
        llm = _structured_trader_llm(captured)
        trader = create_trader(llm)
        trader(_make_trader_state())
        prompt = captured["prompt"]
        user = " ".join(m["content"] for m in prompt if m["role"] == "user")
        system = " ".join(m["content"] for m in prompt if m["role"] == "system")
        assert "Research Manager's assessment:" in user
        assert QUANT_REPORT in user
        assert "already decided the action (BUY)" in system
        assert vn_context.NO_INVENT_RULE in system

    def test_prompt_includes_market_report_for_price_levels(self):
        # #1167: the Trader must see the technical market report so its notes
        # are grounded in real price structure, not just the digested plan.
        captured = {}
        trader = create_trader(_structured_trader_llm(captured))
        trader(_make_trader_state())
        user = " ".join(m["content"] for m in captured["prompt"] if m["role"] == "user")
        assert "Technical Market Report:" in user
        assert "ATR 14 phiên 1.200" in user            # the actual report content reached the Trader
        assert "hỗ trợ 67.000, kháng cự 78.000" in user

    def test_empty_market_report_omits_the_section(self):
        # #1167: when the market analyst wasn't selected the report is empty, so
        # the prompt does not present an empty technical section.
        captured = {}
        state = _make_trader_state()
        state["market_report"] = ""
        create_trader(_structured_trader_llm(captured))(state)
        text = " ".join(m["content"] for m in captured["prompt"])
        assert "Technical Market Report:" not in text
        assert "Research Manager's assessment:" in text  # still present

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        """Free text keeps the model's prose, but the action line, the plan and
        the final proposal are the engine's, even when the prose disagrees."""
        plain_response = "Guidance cut hits margins; I would sell."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        trader = create_trader(llm)
        plan = trader(_make_trader_state())["trader_investment_plan"]
        assert plan.startswith("**Action**: BUY")
        assert plain_response in plan
        assert "**Stop Loss**: 67.000 VND" in plan
        assert plan.rstrip().endswith("FINAL TRANSACTION PROPOSAL: **BUY**")

    def test_without_an_engine_decision_the_trader_proposes_no_trade(self):
        captured = {}
        state = _make_trader_state()
        del state["quant_decision"], state["quant_report"]
        plan = create_trader(_structured_trader_llm(captured))(state)["trader_investment_plan"]
        assert plan.startswith("**Action**: NO_TRADE")
        for field in PLAN_FIELDS:
            assert f"**{field}**: not provided" in plan


# ---------------------------------------------------------------------------
# Research Manager agent: structured happy path + fallback
# ---------------------------------------------------------------------------


def _make_rm_state(action="WATCH"):
    return {
        "company_of_interest": "FPT",
        "investment_debate_state": {
            "history": "Bull and bear arguments here.",
            "bull_history": "Bull says...",
            "bear_history": "Bear says...",
            "current_response": "",
            "count": 1,
        },
        "quant_decision": {"action": action, "exchange": "HOSE"},
        "quant_report": f"ENGINE REPORT: FPT {action}",
    }


def _structured_rm_llm(captured: dict, plan: ResearchPlan | None = None):
    if plan is None:
        plan = ResearchPlan(
            stance=Stance.AGREE,
            rationale="Balanced view across both sides.",
            strategic_actions="Chờ KQKD; theo dõi mốc đã cho.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or plan
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestResearchManagerAgent:
    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        plan = ResearchPlan(
            stance=Stance.DISAGREE,
            rationale="Bull case is stronger; AI tailwind intact.",
            strategic_actions="Theo dõi khối ngoại trong hai tuần.",
        )
        llm = _structured_rm_llm(captured, plan)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state("WATCH"))
        ip = result["investment_plan"]
        # Disagreement is recorded, but the action stays the engine's.
        assert ip.startswith("**Recommendation**: WATCH")
        assert "**Stance on the system action**: disagree" in ip
        assert "**Rationale**: Bull case" in ip
        assert "**Strategic Actions**: Theo dõi" in ip
        assert result["investment_debate_state"]["current_response"] == ip

    def test_prompt_carries_the_fixed_action_and_the_stance_scale(self):
        """The RM judges the debate against the engine's action; it is not
        offered a rating scale to pick from."""
        from tradingagents.agents import vn_context

        captured = {}
        llm = _structured_rm_llm(captured)
        rm = create_research_manager(llm)
        rm(_make_rm_state("SELL"))
        prompt = captured["prompt"]
        assert "The action SELL is fixed by the quantitative system" in prompt
        assert "ENGINE REPORT: FPT SELL" in prompt
        for stance in ("agree", "partly_agree", "disagree"):
            assert stance in prompt
        assert vn_context.NO_INVENT_RULE in prompt
        assert vn_context.GROUNDING_RULES in prompt
        for retired in ("Overweight", "Underweight", "Rating Scale"):
            assert retired not in prompt

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        plain_response = "**Stance on the system action**: agree\n\n**Rationale**: ...\n\n**Strategic Actions**: ..."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state())
        assert result["investment_plan"] == plain_response


# ---------------------------------------------------------------------------
# Sentiment Analyst: schema, render, structured happy path + fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderSentimentReport:
    def test_header_contains_band_and_score(self):
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH,
            overall_score=7.2,
            confidence="high",
            narrative="Source breakdown here.",
        )
        md = render_sentiment_report(report)
        assert "**Overall Sentiment:** **Bullish**" in md
        assert "(Score: 7.2/10)" in md

    def test_header_contains_confidence(self):
        report = SentimentReport(
            overall_band=SentimentBand.NEUTRAL,
            overall_score=5.0,
            confidence="low",
            narrative="Limited data.",
        )
        assert "**Confidence:** Low" in render_sentiment_report(report)

    def test_narrative_preserved_in_output(self):
        narrative = "## Breakdown\n\nCafeF: 3 tin tích cực ✅.\n\n| Signal | Direction |\n|---|---|\n| News | Neutral |"
        report = SentimentReport(
            overall_band=SentimentBand.MILDLY_BULLISH,
            overall_score=6.0,
            confidence="medium",
            narrative=narrative,
        )
        assert narrative in render_sentiment_report(report)

    def test_all_six_bands_render(self):
        for band in SentimentBand:
            report = SentimentReport(
                overall_band=band, overall_score=5.0,
                confidence="medium", narrative="n",
            )
            assert band.value in render_sentiment_report(report)

    def test_score_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            SentimentReport(
                overall_band=SentimentBand.BULLISH, overall_score=11.0,
                confidence="high", narrative="n",
            )


def _make_sentiment_state():
    return {
        "company_of_interest": "FPT",
        "trade_date": "2026-01-15",
        "asset_type": "stock",
        "messages": [],
    }


def _structured_sentiment_llm(captured: dict, report: SentimentReport | None = None):
    """MagicMock LLM whose structured binding captures the prompt and returns
    a real SentimentReport so render_sentiment_report works."""
    if report is None:
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH, overall_score=7.5,
            confidence="high",
            narrative="Tin công bố tích cực. Báo chí lạc quan. Khối ngoại mua ròng.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or report
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestSentimentAnalystAgent:
    @pytest.fixture(autouse=True)
    def _stub_prefetched_sources(self, monkeypatch):
        """Stub the sources the analyst pre-fetches before prompting.

        create_sentiment_analyst fetches company news, a keyword baseline,
        broker research and foreign ownership itself, so without this these
        tests hit the live network.
        """
        from tradingagents.agents.analysts import sentiment_analyst as sentiment

        monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)
        monkeypatch.setattr(sentiment, "_keyword_block", lambda *a, **k: "kw")
        monkeypatch.setattr(sentiment, "_research_block", lambda *a, **k: "research")
        monkeypatch.setattr(sentiment, "_foreign_block", lambda *a, **k: "foreign")

    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        report = SentimentReport(
            overall_band=SentimentBand.MILDLY_BEARISH, overall_score=4.0,
            confidence="medium", narrative="Mixed signals across sources.",
        )
        analyst = create_sentiment_analyst(_structured_sentiment_llm(captured, report))
        sr = analyst(_make_sentiment_state())["sentiment_report"]
        assert "**Overall Sentiment:** **Mildly Bearish**" in sr
        assert "(Score: 4.0/10)" in sr
        assert "Mixed signals across sources." in sr

    def test_sentiment_report_also_in_messages(self):
        captured = {}
        analyst = create_sentiment_analyst(_structured_sentiment_llm(captured))
        result = analyst(_make_sentiment_state())
        assert len(result["messages"]) == 1
        assert result["sentiment_report"] == result["messages"][0].content

    def test_prompt_contains_ticker(self):
        captured = {}
        create_sentiment_analyst(_structured_sentiment_llm(captured))(_make_sentiment_state())
        assert any("FPT" in str(m) for m in captured["prompt"])

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        plain = "**Overall Sentiment:** **Bearish** (Score: 3.0/10)\n**Confidence:** Low\n\nLimited data."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain)
        assert create_sentiment_analyst(llm)(_make_sentiment_state())["sentiment_report"] == plain

    def test_falls_back_to_freetext_when_structured_call_fails(self):
        plain = "Fallback free-text sentiment."
        structured = MagicMock()
        structured.invoke.side_effect = ValueError("bad JSON from model")
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        llm.invoke.return_value = MagicMock(content=plain)
        assert create_sentiment_analyst(llm)(_make_sentiment_state())["sentiment_report"] == plain


@pytest.mark.unit
@pytest.mark.parametrize("source", [
    pytest.param(lambda: inspect.getsource(create_research_manager), id="research_manager prompt"),
    pytest.param(lambda: inspect.getsource(create_portfolio_manager), id="portfolio_manager prompt"),
])
def test_conflict_in_the_debate_cannot_change_the_action(source):
    # The debate always contains conflicting arguments; a decision site that let
    # conflict choose Hold swallowed directional calls on every run (#1321). The
    # action is now the engine's, and both decision sites say the model may not
    # change it: disagreement is recorded as a stance or a risk instead.
    text = " ".join(source().split())
    assert "fixed by the quantitative system" in text
    assert "materially conflicting" not in text


@pytest.mark.unit
def test_no_decision_schema_lets_the_model_choose_the_action():
    for model in (ResearchPlan, TraderProposal, PortfolioDecision):
        fields = set(model.model_fields)
        assert not fields & {"rating", "recommendation", "action", "price_target",
                             "entry_price", "stop_loss"}, model.__name__


@pytest.mark.unit
@pytest.mark.parametrize("written", ["150-160", "150 to 160", "around 150", "150/160", "~150"])
def test_a_price_written_as_a_range_is_dropped(written):
    """Anything that is not a single number becomes None. Letting it through
    fails the whole decision's validation, and the run falls back to free text,
    losing every other field the model got right."""
    assert _coerce_optional_float(written) is None


@pytest.mark.unit
def test_a_price_that_is_a_number_survives():
    assert _coerce_optional_float("$1,150.25") == 1150.25
    assert _coerce_optional_float("56.300 đ") == 56300.0


@pytest.mark.unit
def test_a_field_the_engine_did_not_give_says_so():
    """An omitted line and a line never asked for read the same to an analyst."""
    from tradingagents.agents.schemas import render_pm_decision

    decision = PortfolioDecision(executive_summary="s", investment_thesis="t", key_risks="k")
    rendered = render_pm_decision(decision, {"action": "HOLD"})
    assert rendered.startswith("**Rating**: HOLD")
    assert "**Price Target**: not provided" in rendered
    assert "**Time Horizon**: not provided" in rendered


@pytest.mark.unit
def test_the_pm_decision_carries_the_engine_rating_and_target():
    from tradingagents.agents.schemas import render_pm_decision

    decision = PortfolioDecision(executive_summary="s", investment_thesis="t", key_risks="k",
                                 time_horizon="2-4 tuần")
    rendered = render_pm_decision(decision, BUY_DECISION)
    assert rendered.startswith("**Rating**: BUY")
    assert "**Price Target**: 78.000 VND" in rendered
    assert "**Key Risks**: k" in rendered
    assert "**Time Horizon**: 2-4 tuần" in rendered
    assert render_pm_decision(decision).startswith("**Rating**: NO_TRADE")


@pytest.mark.unit
def test_the_trader_names_the_levels_it_did_not_give():
    rendered = render_trader_proposal(TraderProposal(reasoning="r", execution_notes="e"), {"action": "HOLD"})
    for field in PLAN_FIELDS:
        assert field in rendered
    assert rendered.lower().count("not provided") == len(PLAN_FIELDS)
