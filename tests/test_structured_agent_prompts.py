"""Agents on the schema-only structured-output path must not invite tool calls (#1130).

`with_structured_output` binds exactly one tool (the schema). A prompt that
primes tool use makes models emit an unknown `web_search` call, which discards
the structured attempt and forces a free-text retry — an extra LLM round trip
and the loss of typed output.

These assert the constraint reaches the *rendered* prompt each agent actually
sends, not merely that the constant is referenced in the module.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import tradingagents.agents.analysts.sentiment_analyst as sentiment
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS
from tradingagents.agents.trader.trader import create_trader


def _capturing_llm(captured: dict, result):
    """LLM whose structured binding records the prompt it was handed."""
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or result
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _prompt_text(prompt) -> str:
    """Flatten a captured prompt (str, message list, or objects) to text."""
    if isinstance(prompt, str):
        return prompt
    parts = []
    for m in prompt:
        parts.append(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
    return "\n".join(str(p) for p in parts)


QUANT = {
    "action": "BUY", "exchange": "HOSE", "entry_low": 70500.0, "entry_high": 71200.0,
    "stop_price": 67000.0, "target_price": 78000.0, "target2": 82000.0,
    "rr_ratio": 2.1, "position_size_pct": 6.0, "suggested_shares": 800,
}
QUANT_REPORT = "ENGINE REPORT: BUY FPT, vùng mua 70.500-71.200 VND"


def _assert_quant_grounding(text: str):
    """Every decision prompt carries the engine's decision and the grounding rules."""
    from tradingagents.agents import vn_context

    assert vn_context.quant_block({"quant_report": QUANT_REPORT}) in text
    assert vn_context.NO_INVENT_RULE in text
    assert vn_context.GROUNDING_RULES in text
    # The old scale and the "standard allocation" wording are gone.
    assert "Overweight" not in text and "Underweight" not in text
    assert "standard allocation" not in text.lower()


@pytest.mark.unit
def test_trader_prompt_states_constraint():
    from tradingagents.agents.schemas import TraderProposal

    captured = {}
    llm = _capturing_llm(captured, TraderProposal(reasoning="x", execution_notes="y"))
    create_trader(llm)({
        "company_of_interest": "FPT",
        "investment_plan": "**Recommendation**: BUY",
        "market_report": "Giá đóng cửa 70.900 VND; ATR 1.200.",
        "quant_decision": QUANT,
        "quant_report": QUANT_REPORT,
    })
    text = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in text
    _assert_quant_grounding(text)


@pytest.mark.unit
def test_research_manager_prompt_states_constraint():
    from tradingagents.agents.schemas import ResearchPlan, Stance

    captured = {}
    llm = _capturing_llm(
        captured,
        ResearchPlan(stance=Stance.AGREE, rationale="x", strategic_actions="y"),
    )
    create_research_manager(llm)({
        "company_of_interest": "FPT",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "count": 1,
        },
        "quant_decision": QUANT,
        "quant_report": QUANT_REPORT,
    })
    text = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in text
    _assert_quant_grounding(text)


@pytest.mark.unit
def test_portfolio_manager_prompt_states_constraint():
    from tradingagents.agents.schemas import PortfolioDecision

    captured = {}
    llm = _capturing_llm(
        captured,
        PortfolioDecision(executive_summary="x", investment_thesis="y", key_risks="z"),
    )
    risk = {
        "history": "h", "aggressive_history": "a", "conservative_history": "c",
        "neutral_history": "n", "current_aggressive_response": "",
        "current_conservative_response": "", "current_neutral_response": "",
        "latest_speaker": "Neutral", "count": 1,
    }
    create_portfolio_manager(llm)({
        "company_of_interest": "FPT",
        "risk_debate_state": risk,
        "investment_plan": "plan",
        "trader_investment_plan": "trader plan",
        "quant_decision": QUANT,
        "quant_report": QUANT_REPORT,
    })
    text = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in text
    _assert_quant_grounding(text)


@pytest.mark.unit
def test_a_prompt_without_an_engine_decision_says_the_default_is_no_trade():
    """When the engine could not run, the prompt says so instead of leaving the
    model to choose an action."""
    from tradingagents.agents.schemas import ResearchPlan, Stance

    captured = {}
    llm = _capturing_llm(captured, ResearchPlan(stance=Stance.AGREE, rationale="x", strategic_actions="y"))
    out = create_research_manager(llm)({
        "company_of_interest": "FPT",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "count": 1,
        },
    })
    assert "NO_TRADE" in _prompt_text(captured["prompt"])
    assert out["investment_plan"].startswith("**Recommendation**: NO_TRADE")


def _stub_sentiment_sources(monkeypatch, calls=None):
    """Pre-fetched sources are stubbed so the prompt builds without network I/O."""
    def _rec(name, value):
        def _f(*a, **k):
            if calls is not None:
                calls.append((name, a))
            return value
        return _f

    monkeypatch.setattr(sentiment.get_news, "func", _rec("news", "NEWS-BLOCK"), raising=False)
    monkeypatch.setattr(sentiment, "_keyword_block", _rec("keyword", "KEYWORD-BLOCK"))
    monkeypatch.setattr(sentiment, "_research_block", _rec("research", "RESEARCH-BLOCK"))
    monkeypatch.setattr(sentiment, "_foreign_block", _rec("foreign", "FOREIGN-BLOCK"))


@pytest.mark.unit
def test_sentiment_prompt_states_constraint(monkeypatch):
    from tradingagents.agents.schemas import SentimentBand, SentimentReport

    _stub_sentiment_sources(monkeypatch)

    captured = {}
    llm = _capturing_llm(captured, SentimentReport(
        overall_band=SentimentBand.BULLISH, overall_score=7.5,
        confidence="high", narrative="n",
    ))
    sentiment.create_sentiment_analyst(llm)({
        "company_of_interest": "FPT", "trade_date": "2026-01-15",
        "asset_type": "stock", "messages": [],
    })
    text = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in text
    # This agent binds no tools, so tool-range wording must not reappear.
    assert "tool-call date ranges" not in text


@pytest.mark.unit
def test_tool_using_analysts_keep_their_date_guidance():
    # The analysts that really do call tools keep the wording that anchors their
    # tool date ranges (#836) — this fix is scoped to no-tool agents.
    import tradingagents.agents.analysts.market_analyst as market
    import tradingagents.agents.analysts.news_analyst as news
    for module in (market, news):
        assert "tool-call date ranges" in inspect.getsource(module)


@pytest.mark.unit
def test_constraint_text_is_unambiguous():
    assert "do not call external tools" in NO_EXTERNAL_TOOLS.lower()
    # No template braces: it is embedded in ChatPromptTemplate strings, where
    # braces would be parsed as input variables.
    assert "{" not in NO_EXTERNAL_TOOLS and "}" not in NO_EXTERNAL_TOOLS


@pytest.mark.unit
def test_the_sentiment_prompt_names_the_sources_searched(monkeypatch):
    """The prompt names the Vietnamese sources it carries and the windows they
    cover, and every pre-fetched block reaches it; the old social sources
    (StockTwits, Reddit) are gone."""
    from tradingagents.agents.schemas import SentimentBand, SentimentReport

    calls = []
    _stub_sentiment_sources(monkeypatch, calls)
    captured = {}
    llm = _capturing_llm(captured, SentimentReport(
        overall_band=SentimentBand.NEUTRAL, overall_score=5.0, confidence="low", narrative="n",
    ))
    sentiment.create_sentiment_analyst(llm)({
        "company_of_interest": "fpt.vn", "trade_date": "2026-01-15",
        "asset_type": "stock", "messages": [],
    })
    text = _prompt_text(captured["prompt"])
    for block in ("NEWS-BLOCK", "KEYWORD-BLOCK", "RESEARCH-BLOCK", "FOREIGN-BLOCK"):
        assert block in text
    assert "disclosures (VCI), broker research (CafeF), Vietnamese press (Google News)" in text
    assert "stocktwits" not in text.lower() and "reddit" not in text.lower()
    # Every source is asked for the normalised code over the run's own windows.
    assert ("news", ("FPT", "2026-01-08", "2026-01-15")) in calls
    assert ("keyword", ("FPT", "2026-01-08", "2026-01-15")) in calls
    assert ("research", ("FPT", "2025-10-17", "2026-01-15")) in calls
    assert ("foreign", ("FPT", "2026-01-15")) in calls
