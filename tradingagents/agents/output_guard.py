"""Output Guard: the code-level check that the LLM agents kept the system's decision.

The prompts tell every agent the action and the numbers are fixed; this node
does not trust them to (port of lananh-main ``_enforce_policy_contract``). It
reads the texts that carry the decision - the Research Manager's assessment,
the Trader's plan and the Portfolio Manager's final decision - and:

- an action label or advice that contradicts the system action ("MUA" when the
  system says NO_TRADE, "nên bán" when it says BUY) drops that text and puts
  the system's own decision summary in its place, with a note;
- a price that appears in none of the data the agents were given puts a
  warning and the system summary ahead of the text, which is kept;
- the final decision always ends with exactly one standard disclaimer, and
  ``final_rating`` is always the system action.

The bull, bear and risk debaters argue sides by design and are not checked.
"""

from __future__ import annotations

import logging

from tradingagents.agents import vn_context
from tradingagents.quant import engine, report_format as rfmt

logger = logging.getLogger(__name__)

CONFLICT_NOTE = (
    "⚠️ Phần diễn giải của AI nêu hành động/kế hoạch khác với hệ thống định lượng nên đã bị "
    "lược bỏ - chỉ dùng bản quyết định của hệ thống dưới đây."
)
UNKNOWN_PRICE_NOTE = (
    "⚠️ Phần diễn giải dưới đây có mốc giá không nằm trong số liệu hệ thống - chỉ dùng các mốc "
    "trong bản quyết định của hệ thống."
)

# The decision lines the renderers write themselves from the engine's output:
# their action and numbers are the system's, not the model's, so they are not checked.
_SYSTEM_LINE_PREFIXES = (
    "**Rating**:", "**Recommendation**:", "**Action**:", "FINAL TRANSACTION PROPOSAL:",
    "**Entry Zone**:", "**Stop Loss**:", "**Target 1**:", "**Target 2**:",
    "**Risk/Reward**:", "**Position Sizing**:", "**Price Target**:",
    "**Invalidation Level**:", "**Support Level**:",
)


def _narrative(text: str) -> str:
    return "\n".join(
        line for line in (text or "").splitlines()
        if not line.strip().startswith(_SYSTEM_LINE_PREFIXES)
    )


def _allowed_text(state) -> str:
    """Everything the agents were given or produced from data: where a price may come from."""
    decision = vn_context.quant_decision(state)
    parts = [
        state.get("quant_report") or "",
        decision.get("deterministic_summary") or "",
        engine.render_summary_from_state(decision),
        state.get("market_report") or "",
        state.get("fundamentals_report") or "",
        state.get("news_report") or "",
        state.get("sentiment_report") or "",
        state.get("portfolio_context") or "",
    ]
    return "\n".join(parts)


def guard_text(text: str, state, *, label: str) -> tuple[str, str | None]:
    """The text to keep for one agent output, and a note when it was changed."""
    if not text:
        return text, None
    decision = vn_context.quant_decision(state)
    action = vn_context.action_of(state)
    summary = decision.get("deterministic_summary") or engine.render_summary_from_state(decision)
    allowed = engine.COMPATIBLE_ACTIONS.get(action, frozenset({action}))
    narrative = _narrative(text)
    conflicts = rfmt.find_action_conflicts(narrative, allowed)
    if conflicts:
        logger.warning("%s contradicted the system action %s: %s", label, action, conflicts[:5])
        head = next((line for line in text.splitlines() if line.strip().startswith(_SYSTEM_LINE_PREFIXES)), "")
        kept = f"{head}\n\n" if head else ""
        return (
            f"{kept}{CONFLICT_NOTE}\n\n{summary}",
            f"{label}: bỏ phần diễn giải vì mâu thuẫn với action {action} ({', '.join(conflicts[:3])})",
        )
    unknown = rfmt.find_unknown_prices(narrative, _allowed_text(state))
    if unknown:
        logger.warning("%s cited prices outside the data: %s", label, unknown[:5])
        return (
            f"{UNKNOWN_PRICE_NOTE}\n\n{summary}\n\n{text}",
            f"{label}: có mốc giá ngoài dữ liệu ({', '.join(rfmt.fmt_price(v) for v in unknown[:5])})",
        )
    return text, None


def guard_early(node, key: str, label: str):
    """``node`` with its ``key`` output checked at once, before later agents read it.

    The Research Manager's plan feeds the Trader, the risk team and the
    Portfolio Manager; the Trader's plan feeds the last two. Checked only at
    the end, a contradiction there would already have shaped every later prompt.
    """
    def guarded_node(state) -> dict:
        out = node(state)
        text = (out or {}).get(key)
        if not text:
            return out
        guarded, note = guard_text(text, {**state, **out}, label=label)
        if note is None:
            return out
        logger.info("Output Guard (early) changed %s", label)
        notes = [n for n in (state.get("guard_notes") or "").splitlines() if n]
        return {**out, key: guarded, "guard_notes": "\n".join([*notes, note])}

    return guarded_node


def create_output_guard():
    def output_guard_node(state) -> dict:
        # Notes from the early checks (after the Research Manager and the Trader) are kept.
        notes: list[str] = [n for n in (state.get("guard_notes") or "").splitlines() if n]
        updates: dict = {}
        for key, label in (
            ("investment_plan", "Research Manager"),
            ("trader_investment_plan", "Trader"),
            ("final_trade_decision", "Portfolio Manager"),
        ):
            guarded, note = guard_text(state.get(key) or "", state, label=label)
            if note:
                notes.append(note)
                updates[key] = guarded
        final = updates.get("final_trade_decision", state.get("final_trade_decision") or "")
        updates["final_trade_decision"] = rfmt.ensure_disclaimer(rfmt.clean_analysis_output(final))
        updates["final_rating"] = vn_context.action_of(state)
        updates["guard_notes"] = "\n".join(notes)
        return updates

    return output_guard_node
