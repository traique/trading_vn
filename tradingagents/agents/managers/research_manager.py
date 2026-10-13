"""Research Manager: judges the bull/bear debate against the system action for the trader.

The action is the quantitative engine's (lananh-main's "Manager" step): the
manager weighs which side argued better, records whether the debate supports
the action, and tells the trader what to watch. It cannot change the action.
"""

from __future__ import annotations

from tradingagents.agents import vn_context
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        history = state["investment_debate_state"].get("history", "")
        investment_debate_state = state["investment_debate_state"]
        action = vn_context.action_of(state)

        prompt = f"""As the Research Manager and debate facilitator, critically evaluate this round of the bull/bear debate on a Vietnamese stock and hand the trader a clear assessment.

{instrument_context}

{vn_context.quant_block(state)}

---

The action {action} is fixed by the quantitative system. Your job is to judge the debate, not to pick the action:
- Decide which side argued better on the evidence, independent of which side spoke first or last.
- State whether the debate supports the system action: agree, partly_agree or disagree. Disagreement is allowed and recorded for the portfolio manager, but it does not change the action.
- Tell the trader what to monitor: the invalidation conditions and levels already given, dated catalysts, liquidity and settlement constraints.

**Debate History:**
{history}

## Output

Write these sections, in this order:

- **Stance on the system action**: agree / partly_agree / disagree
- **Rationale**: which arguments were strongest and why
- **Strategic Actions**: what the trader must watch while applying the action

{vn_context.NO_INVENT_RULE}

{vn_context.GROUNDING_RULES}

{NO_EXTERNAL_TOOLS}""" + get_language_instruction()

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            lambda plan: render_research_plan(plan, action),
            "Research Manager",
        )

        new_investment_debate_state = {
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
