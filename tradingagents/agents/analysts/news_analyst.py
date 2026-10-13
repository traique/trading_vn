from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents import vn_context
from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_global_news,
    get_market_context,
    get_news,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_news,
    get_global_news,
    get_market_context,
)


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            "You are a news researcher covering the Vietnamese stock market. Write a comprehensive report of the news and market backdrop of the past week that matters for this stock. Use the available tools: get_news(start_date, end_date) for company news (disclosures, broker research, press), get_global_news(curr_date, look_back_days, limit) for Vietnamese macro and market headlines (State Bank of Vietnam policy and rates, USD/VND, CPI, FDI, exports, foreign flows, market upgrade, public investment), and get_market_context(curr_date) for VN-Index/VN30/HNX/UPCoM performance, the VNINDEX regime and the sector rotation. Separate news of the company itself (marked ✅) from sector or market news (marked ⚠️), and date every item. Provide specific insights with supporting evidence; another agent decides the trade."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
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
            "news_report": report,
        }

    return news_analyst_node
