"""Trader: turns the system decision into an execution plan under Vietnamese market rules.

The action, entry zone, stop, targets and position size are the quantitative
engine's; the trader explains them against the research plan and the market
report and plans the execution (order sessions, board lots, price band, T+2
settlement). The rendered proposal carries the engine's numbers, never the
model's.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tradingagents.agents import vn_context
from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.schemas import TraderProposal, plan_lines, render_trader_proposal
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured,
)


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state):
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]
        market_report = (state["market_report"] or "").strip()
        portfolio_context = get_portfolio_context_from_state(state)
        decision = vn_context.quant_decision(state)
        action = vn_context.action_of(state)
        report_section = f"Technical Market Report:\n{market_report}\n\n" if market_report else ""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the trader of a Vietnamese equity desk. The quantitative system has "
                    f"already decided the action ({action}) and every number of the plan; you explain "
                    "the plan and how to execute it, and you may not change the action or any number. "
                    + vn_context.market_rules(state)
                    + "\n\n" + vn_context.NO_INVENT_RULE
                    + "\n\n" + vn_context.GROUNDING_RULES
                    + "\n" + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Here is the research team's assessment for {company_name}. "
                    f"{instrument_context}\n\n"
                    f"{vn_context.quant_block(state)}\n\n"
                    f"{report_section}"
                    f"{portfolio_context}\n\n"
                    f"Research Manager's assessment:\n{investment_plan}\n\n"
                    "## Output\n\n"
                    "- **Reasoning**: why the evidence supports (or strains) the system action\n"
                    "- **Execution Notes**: how to execute it - which session (ATO / continuous / "
                    "ATC), splitting into board lots of 100, the price band and tick size, the T+2 "
                    "settlement constraint, liquidity. If the action has no plan, say what would have "
                    "to change for the system to issue one."
                ),
            },
        ]

        proposal = invoke_structured(structured_llm, messages, "Trader")
        if proposal is not None:
            trader_plan = render_trader_proposal(proposal, decision)
        else:
            # Free text keeps the model's prose, but the numbers and the action
            # line are always the engine's.
            prose = llm.invoke(messages).content
            trader_plan = "\n".join([
                f"**Action**: {action} (hành động của hệ thống định lượng)",
                "",
                prose,
                "",
                *plan_lines(decision),
                "",
                f"FINAL TRANSACTION PROPOSAL: **{action}**",
            ])

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
        }

    return trader_node
