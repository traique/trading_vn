"""Company and market news for Vietnamese stocks, dated and windowed.

Company news merges three feeds, each item tagged by how sure it is about the
ticker (the analysts may only treat ✅ items as news *of* the company):

- ✅ VCI disclosures (``/v1/news``): filed by the company itself.
- ✅ CafeF broker research notes on the ticker.
- Google News headlines for "<MÃ> cổ phiếu": ✅ when the headline names the
  ticker, ⚠️ (sector/market news) when it does not.

Every item is dated and inside the requested window; an empty result over a
reachable window is a real absence of news.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import vci, vn_news
from tradingagents.market import vn_rules
from tradingagents.quant import report_format as rfmt

logger = logging.getLogger(__name__)

# Lexicon sentiment of a headline (port from lananh-main providers.sentiment_score):
# a crude, deterministic signal fed to the policy's target projection, never a verdict.
POS_WORDS = ["tăng", "lãi", "kỷ lục", "bứt phá", "tích cực", "mua ròng", "vượt", "khởi sắc", "tăng trưởng", "cổ tức"]
NEG_WORDS = ["giảm", "lỗ", "bán ròng", "sụt", "tiêu cực", "thua lỗ", "cảnh báo", "rủi ro", "vi phạm", "phạt", "đình chỉ"]
NEGATION_WORDS = ["không", "chưa", "chẳng", "ngừng", "hết"]


def sentiment_score(title: str) -> float:
    """-1..1 from positive/negative keywords, a negation within 20 chars flips the sign."""
    lowered = (title or "").lower()
    score = 0.0
    for words, sign in ((POS_WORDS, 1), (NEG_WORDS, -1)):
        for word in words:
            idx = lowered.find(word)
            if idx < 0:
                continue
            window = lowered[max(0, idx - 20):idx]
            score += -sign if any(n in window for n in NEGATION_WORDS) else sign
    return max(-1.0, min(1.0, score / 3))


def _vci_items(symbol: str, start_date: str, end_date: str) -> list[vn_news.Headline]:
    items = []
    for row in vci.fetch_news(symbol, start_date, end_date):
        raw = row.get("publicDate")
        try:
            published = datetime.fromisoformat(str(raw)[:19]).replace(tzinfo=vn_rules.VN_TZ).astimezone(UTC)
        except (TypeError, ValueError):
            continue
        if not (start_date <= f"{published.astimezone(vn_rules.VN_TZ):%Y-%m-%d}" <= end_date):
            continue
        items.append(vn_news.Headline(
            title=str(row.get("newsTitle") or "").strip(),
            source=str(row.get("newsSource") or "Công bố thông tin (VCI)"),
            published=published,
            summary=str(row.get("newsShortContent") or "")[:400],
            confirmed=True,
        ))
    return items


NEWS_FEEDS = 4   # VCI disclosures, CafeF research, CafeF ticker news, Google News


def collect_company_news(symbol: str, start_date: str, end_date: str, limit: int) -> tuple[list[vn_news.Headline], list[str]]:
    """Merged, de-duplicated company news, newest first, plus the feeds that failed."""
    feeds = {
        "VCI": lambda: _vci_items(symbol, start_date, end_date),
        "CafeF": lambda: vn_news.cafef_research(symbol, start_date, end_date),
        "CafeF tin": lambda: vn_news.cafef_company_news(symbol, start_date, end_date, limit=limit),
        "Google News": lambda: vn_news.google_news(f'"{symbol}" cổ phiếu', start_date, end_date, limit=limit),
    }
    items: list[vn_news.Headline] = []
    failed: list[str] = []
    for name, fetch in feeds.items():
        try:
            items.extend(fetch())
        except VendorUnavailableError as exc:
            logger.warning("%s news unavailable for %s: %s", name, symbol, exc)
            failed.append(name)
        except Exception as exc:  # noqa: BLE001 - one feed's parse error must not sink the others
            logger.warning("%s news failed for %s: %s", name, symbol, exc)
            failed.append(name)
    seen: set[str] = set()
    unique = []
    for item in items:
        key = item.title.lower()
        if not item.title or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda h: (rfmt.is_news_relevant(h.title, symbol, h.confirmed), h.published), reverse=True)
    return unique[:limit], failed


def _render(items: list[vn_news.Headline], symbol: str | None) -> list[str]:
    lines = []
    for item in items:
        day = item.published.astimezone(vn_rules.VN_TZ).strftime("%d/%m/%Y") if item.published else "không rõ ngày"
        if symbol is None:
            mark = ""
        else:
            mark = "✅ " if rfmt.is_news_relevant(item.title, symbol, item.confirmed) else "⚠️ "
        lines.append(f"### {mark}{item.title}")
        lines.append(f"({item.source}, {day})")
        if item.summary:
            lines.append(item.summary)
        lines.append("")
    return lines


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Company news in ``[start_date, end_date]``."""
    symbol = normalize_symbol(ticker)
    limit = int(get_config().get("news_article_limit", 20))
    items, failed = collect_company_news(symbol, start_date, end_date, limit)
    if not items and len(failed) == NEWS_FEEDS:
        raise VendorUnavailableError(f"every news feed failed for {symbol}")
    lines = [
        f"## Tin tức {symbol} từ {start_date} đến {end_date}",
        "Đánh dấu: ✅ tin của chính doanh nghiệp/đúng mã; ⚠️ tin ngành/thị trường chung, "
        "KHÔNG được gán thành sự kiện của mã.",
        "",
    ]
    if failed:
        lines.insert(1, f"(Không truy cập được nguồn: {', '.join(failed)} - thiếu tin không có nghĩa là không có sự kiện.)")
    if not items:
        lines.append(f"Không có tin nào về {symbol} trong khoảng này từ các nguồn còn truy cập được.")
        return "\n".join(lines)
    return "\n".join(lines + _render(items, symbol))


def get_global_news(as_of_date: str, look_back_days: int | None = None, limit: int | None = None) -> str:
    """Vietnamese macro and market headlines in the days before ``as_of_date``."""
    config = get_config()
    look_back = int(look_back_days or config.get("global_news_lookback_days", 7))
    limit = int(limit or config.get("global_news_article_limit", 10))
    end = datetime.strptime(as_of_date, "%Y-%m-%d")
    start = (end - timedelta(days=look_back)).strftime("%Y-%m-%d")
    queries = config.get("global_news_queries") or []
    items: list[vn_news.Headline] = []
    failed = 0
    per_query = max(3, limit // max(1, len(queries)) + 1)
    for query in queries:
        try:
            items.extend(vn_news.google_news(query, start, as_of_date, limit=per_query))
        except VendorUnavailableError:
            failed += 1
    if not items and queries and failed == len(queries):
        raise VendorUnavailableError("Google News unavailable for every macro query")
    seen: set[str] = set()
    unique = []
    for item in sorted(items, key=lambda h: h.published, reverse=True):
        if item.title.lower() in seen:
            continue
        seen.add(item.title.lower())
        unique.append(item)
    lines = [f"## Tin vĩ mô & thị trường Việt Nam từ {start} đến {as_of_date}", ""]
    if not unique:
        lines.append("Không có tin vĩ mô nào trong khoảng này.")
        return "\n".join(lines)
    return "\n".join(lines + _render(unique[:limit], None))


def get_broker_research(ticker: str, start_date: str, end_date: str, limit: int = 5) -> str:
    """Brokers' research notes on the ticker published in the window (CafeF), as text."""
    symbol = normalize_symbol(ticker)
    try:
        notes = vn_news.cafef_research(symbol, start_date, end_date, limit=limit)
    except VendorUnavailableError as exc:
        return f"<broker research unavailable: {exc}>"
    if not notes:
        return f"Không có báo cáo phân tích nào của công ty chứng khoán về {symbol} trong khoảng này."
    lines = []
    for note in notes:
        day = note.published.astimezone(vn_rules.VN_TZ).strftime("%d/%m/%Y") if note.published else "không rõ ngày"
        lines.append(f"- {note.title} ({note.source}, {day}): {note.summary}")
    return "\n".join(lines)


def get_foreign_ownership(ticker: str, as_of_date: str) -> str:
    """Foreign ownership and remaining room, for a run dated today only (VCI keeps no history)."""
    from tradingagents.dataflows.date_window import is_historical

    if is_historical(as_of_date):
        return "Không dùng cho lượt phân tích ngày quá khứ: VCI chỉ có tỷ lệ sở hữu nước ngoài hiện tại."
    symbol = normalize_symbol(ticker)
    try:
        details = vci.fetch_details(symbol)
    except (VendorUnavailableError, NoMarketDataError) as exc:
        return f"<foreign ownership unavailable: {exc}>"
    held, cap = details.get("foreignerPercentage"), details.get("maximumForeignPercentage")
    if held is None or not cap:
        return "Không có dữ liệu sở hữu nước ngoài."
    return (
        f"Khối ngoại sở hữu {rfmt.fmt_pct(held * 100)} / room tối đa {rfmt.fmt_pct(cap * 100)} "
        f"(room còn {rfmt.fmt_pct((cap - held) * 100)}). Đây là tỷ lệ hiện tại, không phải dòng tiền mua/bán ròng."
    )


def get_live_match_price(ticker: str, day: str) -> float | None:
    """The last matched price of today's session (DNSE), or None when there is none."""
    from tradingagents.dataflows.vendors import dnse

    try:
        return dnse.fetch_last_match(normalize_symbol(ticker), day)
    except VendorUnavailableError:
        return None
