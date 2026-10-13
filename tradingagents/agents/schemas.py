"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- The action and every price level come from the quantitative engine; the
  schemas only ask the model for the explanation
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# LLMs sometimes write a placeholder string ("None", "N/A", ...) into an optional
# numeric field instead of omitting it. Coerce those to None so the structured
# call validates instead of erroring (#1058). Pydantic still parses real numeric
# strings ("189.5") to float.
_NULLISH_FLOAT = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _coerce_optional_float(value):
    """Normalise an LLM-written optional numeric field before validation.

    Three shapes show up in practice: a placeholder string ("None", "N/A") in
    place of an omitted value (#1058); a percentage where a price was asked for
    ("15%", #1288); and a human-formatted price. A percentage cannot be salvaged
    into an absolute level, so it is dropped like a placeholder. A formatted
    price is reduced to its number, reading Vietnamese notation too: "70.500",
    "70.500 đ", "70.500 VND", "70,5 nghìn" (thousand) all mean 70500.

    Anything that is not a single number is dropped the same way. A range
    ("150-160") or a hedge ("around 150") would otherwise reach pydantic, fail
    validation, and discard the whole decision.
    """
    if not isinstance(value, str):
        return value
    text = value.strip().lower()
    if text in _NULLISH_FLOAT or text.endswith("%"):
        return None
    scale = 1.0
    for word in ("nghìn", "ngàn", "k"):
        if text.endswith(word):
            text, scale = text[: -len(word)].strip(), 1000.0
            break
    for unit in ("vnd", "vnđ", "đồng", "đ", "₫", "$", "€", "£", "¥"):
        text = text.replace(unit, "")
    text = text.strip()
    if "." in text and "," in text:
        # Vietnamese "1.234,5" or English "1,234.5": the last separator is the decimal.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "." in text and all(len(part) == 3 for part in text.split(".")[1:]):
        text = text.replace(".", "")  # 70.500 / 1.234.500: thousands separators
    elif "," in text and all(len(part) == 3 for part in text.split(",")[1:]):
        text = text.replace(",", "")  # 70,500: English thousands separator
    else:
        text = text.replace(",", ".")  # 70,5: Vietnamese decimal comma
    try:
        return float(text) * scale
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Shared action type
# ---------------------------------------------------------------------------


class SystemAction(StrEnum):
    """The five actions of the quantitative engine (port of lananh-main's policy).

    The engine decides the action; every agent explains it and none may change
    it. SELL only exits a holding: Vietnamese shares cannot be sold short.
    """

    BUY = "BUY"
    HOLD = "HOLD"
    WATCH = "WATCH"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


# Kept for callers that name the rating type; the scale is now SystemAction.
PortfolioRating = SystemAction


class Stance(StrEnum):
    """How strongly an agent's reading of the evidence supports the system action."""

    AGREE = "agree"
    PARTLY_AGREE = "partly_agree"
    DISAGREE = "disagree"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured assessment produced by the Research Manager.

    The action is the quantitative engine's; the manager judges how the bull and
    bear debate bears on it and what the trader must watch. Disagreement is
    allowed and recorded, but it does not change the action.
    """

    stance: Stance = Field(
        description=(
            "Whether the debate supports the system action: agree, partly_agree or "
            "disagree. Disagreeing is allowed and recorded; it does not change the action."
        ),
    )
    rationale: str = Field(
        description=(
            "Which arguments of the bull and bear sides were strongest and why, "
            "anchored in specific data from the reports. Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "What the trader should monitor while applying the system action: the "
            "invalidation level and conditions already given, catalysts and dates, "
            "liquidity and settlement constraints. Use only price levels already given."
        ),
    )


def render_research_plan(plan: ResearchPlan, action: str = "NO_TRADE") -> str:
    """Render a ResearchPlan to markdown, led by the system action it assesses."""
    return "\n".join([
        f"**Recommendation**: {action} (hành động của hệ thống định lượng)",
        "",
        f"**Stance on the system action**: {plan.stance.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured execution note produced by the Trader.

    The action, entry zone, stop, targets and size come from the quantitative
    engine and are filled in by the node; the trader explains them and plans
    the execution under Vietnamese market rules.
    """

    reasoning: str = Field(
        description=(
            "The case for the system action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    execution_notes: str = Field(
        description=(
            "How to execute the system plan under Vietnamese market rules: order "
            "timing (ATO / continuous / ATC), board lots of 100 shares, the price "
            "band and tick size, T+2 settlement (no exit before the third session), "
            "liquidity. Use only the price levels already given; if the action has "
            "no plan, say what would have to change for one."
        ),
    )


def _vnd(value) -> str:
    from tradingagents.quant import report_format as rfmt

    return f"{rfmt.fmt_price(value)} VND" if value is not None else "not provided"


def plan_lines(decision: dict) -> list[str]:
    """The engine's trade-plan fields as labelled markdown lines (always all named)."""
    from tradingagents.quant import report_format as rfmt

    if decision.get("entry_low") is not None:
        entry = f"{_vnd(decision['entry_low'])} - {_vnd(decision.get('entry_high'))}"
    else:
        entry = "not provided"
    size = decision.get("position_size_pct")
    sizing = "not provided"
    if size is not None:
        sizing = f"{rfmt.fmt_number(size, 1)}% NAV"
        if decision.get("suggested_shares"):
            sizing += f" (~{rfmt.fmt_price(decision['suggested_shares'])} cổ phiếu, lô 100)"
    rr = decision.get("rr_ratio")
    return [
        f"**Entry Zone**: {entry}",
        f"**Stop Loss**: {_vnd(decision.get('stop_price'))}",
        f"**Target 1**: {_vnd(decision.get('target_price'))}",
        f"**Target 2**: {_vnd(decision.get('target2'))}",
        f"**Risk/Reward**: {rfmt.fmt_number(rr) if rr is not None else 'not provided'}",
        f"**Position Sizing**: {sizing}",
    ]


def render_trader_proposal(proposal: TraderProposal, decision: dict | None = None) -> str:
    """Render a TraderProposal to markdown with the engine's numbers.

    The trailing ``FINAL TRANSACTION PROPOSAL: **ACTION**`` line is preserved
    for backward compatibility with code that greps for it.
    """
    decision = decision or {}
    action = decision.get("action") or "NO_TRADE"
    parts = [
        f"**Action**: {action} (hành động của hệ thống định lượng)",
        "",
        f"**Reasoning**: {proposal.reasoning}",
        "",
        f"**Execution Notes**: {proposal.execution_notes}",
        "",
        *plan_lines(decision),
        "",
        f"FINAL TRANSACTION PROPOSAL: **{action}**",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The rating and price target are the quantitative engine's and are filled in
    by the node; the manager writes the summary, the thesis and the risks.
    """

    executive_summary: str = Field(
        description=(
            "A concise statement of the system action and how to act on it: the "
            "entry, sizing, stop and targets already given, and the time horizon. "
            "Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts and "
            "the risk debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    key_risks: str = Field(
        description=(
            "Two or three risks that would make the system's scenario wrong, with "
            "the invalidation level already given where there is one."
        ),
    )
    time_horizon: str | None = Field(
        default=None,
        description="Optional recommended holding period, e.g. '2-4 tuần'.",
    )


def render_pm_decision(decision: PortfolioDecision, system: dict | None = None) -> str:
    """Render a PortfolioDecision to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output keeps the ``**Rating**`` header first.
    """
    system = system or {}
    parts = [
        f"**Rating**: {system.get('action') or 'NO_TRADE'}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
        "",
        f"**Key Risks**: {decision.key_risks}",
        "",
        f"**Price Target**: {_vnd(system.get('target_price'))}",
        "",
        f"**Time Horizon**: {decision.time_horizon or 'not provided'}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sentiment Analyst
# ---------------------------------------------------------------------------


class SentimentBand(StrEnum):
    """Discrete sentiment direction produced by the Sentiment Analyst.

    Six tiers keep the signal granular enough to be actionable while remaining
    small enough for every provider to map reliably from its JSON output.
    """

    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


class SentimentReport(BaseModel):
    """Structured sentiment report produced by the Sentiment Analyst.

    Replaces the previous free-form prose output so downstream consumers
    (dashboards, audit logs, PDF renderers, other agents) can read
    ``overall_band`` and ``overall_score`` without maintaining fragile regex
    fallbacks that drift with every model release. ``narrative`` preserves the
    rich source-by-source analysis; ``render_sentiment_report`` prepends a
    deterministic header so the saved report stays human-readable.
    """

    overall_band: SentimentBand = Field(
        description=(
            "Overall sentiment direction. Exactly one of: "
            "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. "
            "Use Mixed when sources point in clearly different directions. "
            "Use Neutral only when all sources are genuinely silent or non-committal."
        ),
    )
    overall_score: float = Field(
        ge=0.0,
        le=10.0,
        description=(
            "Numeric sentiment intensity on a 0–10 scale. "
            "0 = maximally bearish, 5 = neutral, 10 = maximally bullish. "
            "Guideline for consistency with overall_band: "
            "Bullish ~6.5–10, Mildly Bullish ~5.5–6.4, Neutral/Mixed ~4.5–5.5, "
            "Mildly Bearish ~3.5–4.4, Bearish ~0–3.4. "
            "Only the 0–10 bounds are enforced."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence in the assessment based on data quality and sample size. "
            "Use 'low' when a source was unavailable or there are fewer than 3 "
            "news items about the company itself; 'medium' when data is present "
            "but sparse; 'high' when company news, research and the keyword "
            "baseline all returned substantive data."
        ),
    )
    narrative: str = Field(
        description=(
            "Full sentiment report covering, in order: "
            "(1) source-by-source breakdown with specific evidence (cite dated "
            "headlines, disclosures and research notes); "
            "(2) cross-source divergences and alignments; "
            "(3) dominant narrative themes; "
            "(4) catalysts and risks surfaced by the data; "
            "(5) a markdown table summarising key sentiment signals, their "
            "direction, source, and supporting evidence. "
            "Keep it informative and substantive: develop each section thoroughly "
            "with concrete evidence so every point adds new signal for the trader."
        ),
    )


def render_sentiment_report(report: SentimentReport) -> str:
    """Render a SentimentReport to the markdown shape the rest of the system expects.

    The structured header (band + score + confidence) is prepended to the
    narrative so the saved report is both human-readable and machine-parseable
    without regex.
    """
    return "\n".join([
        f"**Overall Sentiment:** **{report.overall_band.value}** "
        f"(Score: {report.overall_score:.1f}/10)",
        f"**Confidence:** {report.confidence.capitalize()}",
        "",
        report.narrative,
    ])
