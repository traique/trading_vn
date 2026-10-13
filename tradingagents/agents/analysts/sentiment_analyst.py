"""Sentiment analyst: one sentiment report from Vietnamese news flow.

The node fetches its sources before calling the model and puts them in the
prompt, so the model reports on data it was given rather than inventing posts:

  1. Company news of the past 7 days: disclosures (VCI), broker research
     (CafeF) and the Vietnamese press (Google News), each item tagged ✅ when it
     is about the company and ⚠️ when it is sector or market news.
  2. A deterministic keyword score of those headlines (port of lananh-main's
     lexicon), so the model weighs its read against a fixed baseline.
  3. Broker research notes on the ticker over the past 90 days.
  4. Foreign ownership and remaining room, for a run dated today only (VCI has
     no history of it).

Every source is dated and windowed to the run date, so a historical run reads
only what was public then. The report is a SentimentReport through structured
output where the provider supports it and free text otherwise, so the band,
score and confidence header reads the same across providers.
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents import vn_context
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.agents.tools import get_news
from tradingagents.dataflows.news import (
    collect_company_news,
    get_broker_research,
    get_foreign_ownership,
    sentiment_score,
)
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.quant import report_format as rfmt


def _days_back(trade_date: str, days: int) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def _keyword_block(ticker: str, start_date: str, end_date: str) -> str:
    """Deterministic headline score: counts and the aggregate of the lexicon score."""
    try:
        items, failed = collect_company_news(ticker, start_date, end_date, limit=20)
    except Exception as exc:  # noqa: BLE001 - the score is a baseline, never required
        return f"<keyword score unavailable: {exc}>"
    relevant = [i for i in items if rfmt.is_news_relevant(i.title, ticker, i.confirmed)]
    scored = [(i.title, sentiment_score(i.title), i.confirmed) for i in items]
    positive = sum(1 for i in relevant if sentiment_score(i.title) > 0.2)
    negative = sum(1 for i in relevant if sentiment_score(i.title) < -0.2)
    lines = [
        f"Tin đúng mã (✅): {len(relevant)} | tích cực: {positive} | tiêu cực: {negative} | "
        f"trung tính: {len(relevant) - positive - negative} | tin chung (⚠️): {len(items) - len(relevant)}",
        "Điểm tác động tin theo từ khoá (-2..+2, chỉ tính tin ✅): "
        + rfmt.fmt_number(rfmt.relevant_news_impact(scored, ticker)),
    ]
    if failed:
        lines.append(f"Nguồn không truy cập được: {', '.join(failed)}")
    return "\n".join(lines)


def _research_block(ticker: str, start_date: str, end_date: str) -> str:
    try:
        return get_broker_research(ticker, start_date, end_date)
    except Exception as exc:  # noqa: BLE001 - research notes are optional
        return f"<broker research unavailable: {exc}>"


def _foreign_block(ticker: str, trade_date: str) -> str:
    try:
        return get_foreign_ownership(ticker, trade_date)
    except Exception as exc:  # noqa: BLE001 - live enrichment only
        return f"<foreign ownership unavailable: {exc}>"


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches the Vietnamese news flow, a keyword baseline, broker research and
    (for a run dated today) foreign ownership, injects them into the prompt, and
    produces a sentiment report via structured output (with a free-text fallback
    for providers that do not support it).
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        ticker = normalize_symbol(state["company_of_interest"])
        end_date = state["trade_date"]
        start_date = _days_back(end_date, 7)
        instrument_context = get_instrument_context_from_state(state)

        # Pre-fetch every source. Each degrades to a placeholder string, so the
        # LLM always sees something - either real data or a clear gap.
        news_block = get_news.func(ticker, start_date, end_date)
        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            keyword_block=_keyword_block(ticker, start_date, end_date),
            research_block=_research_block(ticker, _days_back(end_date, 90), end_date),
            foreign_block=_foreign_block(ticker, end_date),
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Report what your tools support; another agent decides the trade."
                    # No tool-calling here: the data is pre-fetched into the
                    # prompt, so tool-range wording would only invite a
                    # hallucinated tool call (#1130).
                    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}"
                    " " + NO_EXTERNAL_TOOLS +
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    keyword_block: str,
    research_block: str,
    foreign_block: str,
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    return f"""You are a market sentiment analyst covering Vietnamese stocks. Produce a sentiment report for {ticker} covering {start_date} to {end_date}, from the data already collected below.

## Data sources (pre-fetched, in this prompt)

### Company news, past 7 days - disclosures (VCI), broker research (CafeF), Vietnamese press (Google News)
Items marked ✅ are about {ticker}; items marked ⚠️ are sector or market news and must not be read as events of {ticker}.

<start_of_news>
{news_block}
<end_of_news>

### Keyword baseline (deterministic, not a verdict)
A fixed lexicon score of the same headlines. Use it as a baseline to check your read against; explain any disagreement.

<start_of_keyword_score>
{keyword_block}
<end_of_keyword_score>

### Broker research notes on {ticker}, past 90 days (CafeF)
Institutional views; each note is the broker's opinion at its date, not a fact.

<start_of_research>
{research_block}
<end_of_research>

### Foreign ownership
<start_of_foreign>
{foreign_block}
<end_of_foreign>

## How to analyze this data

1. **Separate events from opinion.** A disclosure (board resolution, quarterly results, dividend) is an event; a press article asking whether the stock is attractive, or a broker note, is opinion. Weight them differently.
2. **Only ✅ items speak about {ticker}.** ⚠️ items describe the sector or the market; use them as backdrop only.
3. **Read the Vietnamese press tone carefully.** Headlines about "khối ngoại bán ròng", "lao dốc", "về vùng đáy" signal selling pressure; "mua ròng", "kỷ lục", "bứt phá" signal enthusiasm. Retail-oriented outlets amplify short-term moves: one story repeated by several outlets is one story, not many.
4. **Look for divergences** between broker research and the press, or between the news tone and the keyword baseline - the mismatch is itself a signal.
5. **Identify the dominant narrative** and upcoming catalysts (results season, dividend ex-dates, AGMs, index rebalancing, FTSE/MSCI upgrade news, foreign flows).
6. **Be honest about data limits.** Few ✅ items, or an unavailable source, means low confidence - say so in `confidence` and the narrative.
7. **Past sentiment is not predictive.** Frame conclusions as a signal for the trader, not a price call.

## Output fields

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when the sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on how many ✅ items there are and whether any source was unavailable.
- **narrative**: Source-by-source breakdown, divergences, dominant themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, date, evidence).

{vn_context.GROUNDING_RULES}

{get_language_instruction()}"""
