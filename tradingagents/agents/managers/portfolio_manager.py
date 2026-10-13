"""Portfolio Manager: writes the final decision around the system action.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call. The rating and price target
are the quantitative engine's, never the model's: ``final_rating`` is the
engine's action, and the decision is rendered to markdown as
``final_trade_decision`` for the memory log, CLI display and saved reports.
When a provider does not expose structured output, the agent falls back to
free-text generation under the engine's rating line.
"""

from __future__ import annotations

from tradingagents.agents import vn_context
from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured
from tradingagents.quant import report_format as rfmt


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        portfolio_context = get_portfolio_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]
        decision = vn_context.quant_decision(state)
        action = vn_context.action_of(state)

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, write the final decision on this Vietnamese stock for the investor.

{instrument_context}

{portfolio_context}

{vn_context.quant_block(state)}

---

The action {action} and every number of the plan are fixed by the quantitative system; you explain them, you do not choose them. Ground every statement in specific evidence from the analysts and the risk debate. Where the debate disagrees with the system, say so plainly as a risk - do not soften it and do not change the action.

**Context:**
- Research Manager's assessment: **{research_plan}**
- Trader's execution plan: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

## Output

- **Executive Summary**: the system action and how to act on it, with the entry, sizing, stop and targets already given (or, for a non-BUY action, why there is none)
- **Investment Thesis**: the evidence that supports or strains the action, and what would change it
- **Key Risks**: two or three risks that would make the scenario wrong, with the invalidation level already given
- **Time Horizon**: optional

{vn_context.NO_INVENT_RULE}

{vn_context.GROUNDING_RULES}

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        result = invoke_structured(structured_llm, prompt, "Portfolio Manager")
        if result is not None:
            final_trade_decision = render_pm_decision(result, decision)
        else:
            prose = llm.invoke(prompt).content
            target = decision.get("target_price")
            final_trade_decision = "\n".join([
                f"**Rating**: {action}",
                "",
                prose,
                "",
                f"**Price Target**: {rfmt.fmt_price(target) + ' VND' if target is not None else 'not provided'}",
            ])

        new_risk_debate_state = {
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "final_rating": action,
        }

    return portfolio_manager_node
