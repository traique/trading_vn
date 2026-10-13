from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents import vn_context
from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_balance_sheet,
    get_cashflow,
    get_corporate_events,
    get_fundamentals,
    get_income_statement,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_corporate_events,
)


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            "You are a fundamental analyst of Vietnamese listed companies. Write a comprehensive report of the company's fundamentals - valuation, profitability, balance-sheet strength, cash flow, growth and recent corporate events - to inform the traders. Value banks, securities firms, insurers and real-estate developers on P/B and ROE (and for banks NIM, NPL, CASA), other companies on P/E, and compare with the sector benchmark the tool provides. Statements are in billions of VND and each period shows the date it was published; only reports published by the trade date exist for this analysis. In Vietnam insider deals are disclosed as registrations (đăng ký) and results (kết quả) of trades by insiders and major shareholders; a registration is not a completed trade. Note dividends and share issues with their ex-rights dates (GDKHQ), since they dilute or move the reference price. Provide specific insights with supporting evidence; another agent decides the trade."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + " Use the available tools: `get_fundamentals` for valuation, profitability, growth and the sector benchmark, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements, and `get_corporate_events` for dividends, share issues, insider/major-shareholder deals and shareholder meetings."
            + "\n\n" + vn_context.GROUNDING_RULES
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, TOOLS, state["messages"])

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
