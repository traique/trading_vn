"""Vietnamese news feeds: Google News RSS (Vietnamese edition) and CafeF.

- Google News search accepts ``after:YYYY-MM-DD before:YYYY-MM-DD`` in the
  query, so a past run reads the headlines of its own window rather than
  today's. Items carry the publisher in ``<source>`` and a ``pubDate``.
- CafeF ``BaoCaoPhanTich.ashx`` lists brokers' research notes on a ticker
  with the date each was published (``DateDeploy``, ASP.NET ``/Date(ms)/``).
- CafeF ``Events_RelatedNews_New.aspx`` lists CafeF's own articles tagged to a
  ticker, newest first, each with its time (``dd/mm/yyyy HH:MM``) - a plain
  HTML fragment, no browser needed. Paged back to reach a past window.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from tradingagents.dataflows.vendors import http
from tradingagents.market import vn_rules

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
CAFEF_TICKER_NEWS = "https://cafef.vn/du-lieu/Ajax/Events_RelatedNews_New.aspx"
_CAFEF_ROW = re.compile(
    r'<span class="timeTitle">\s*(\d{2}/\d{2}/\d{4} \d{2}:\d{2})\s*</span>[\s\S]*?'
    r'<a[^>]*href="([^"]+)"[^>]*title="([^"]+)"',
    re.IGNORECASE,
)
CAFEF_MAX_PAGES = 6
CAFEF_REPORTS = "https://cafef.vn/du-lieu/Ajax/PageNew/BaoCaoPhanTich.ashx"

# Covered-warrant noise: a "CFPT2501" headline is about a warrant, not the stock.
NOISE_KEYWORDS = ("chứng quyền", "cw ", "cmw")


@dataclass
class Headline:
    title: str
    source: str
    published: datetime | None  # UTC-aware
    url: str = ""
    summary: str = ""
    confirmed: bool | None = None  # True: the publisher tagged it to the ticker


def _tag(xml: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", xml, re.IGNORECASE)
    if not match:
        return ""
    text = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", match.group(1)).strip()
    return html.unescape(text)


def _parse_rfc822(raw: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def google_news(query: str, start_date: str, end_date: str, limit: int = 20) -> list[Headline]:
    """Headlines for ``query`` published in ``[start_date, end_date]``, newest first.

    The date operators narrow Google's own search; the window is re-applied to
    each item's ``pubDate`` since the operators are only advisory.
    """
    before = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    text = http.get_text(
        GOOGLE_NEWS_RSS,
        vendor="Google News",
        params={"q": f"{query} after:{start_date} before:{before}", "hl": "vi", "gl": "VN", "ceid": "VN:vi"},
        headers={"Accept": "application/rss+xml, application/xml, text/xml"},
    )
    window_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=vn_rules.VN_TZ)
    window_end = datetime.strptime(before, "%Y-%m-%d").replace(tzinfo=vn_rules.VN_TZ)
    seen: set[str] = set()
    items: list[Headline] = []
    for raw in re.findall(r"<item>[\s\S]*?</item>", text, re.IGNORECASE):
        full_title = _tag(raw, "title")
        source = _tag(raw, "source")
        title = full_title
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        title = title.strip()
        key = title.lower()
        if len(title) <= 5 or key in seen or any(k in key for k in NOISE_KEYWORDS):
            continue
        published = _parse_rfc822(_tag(raw, "pubDate"))
        if published is None or not (window_start <= published < window_end):
            continue
        seen.add(key)
        items.append(Headline(title=title, source=source, published=published, url=_tag(raw, "link")))
    items.sort(key=lambda h: h.published, reverse=True)
    return items[:limit]


def _asp_date(raw: str | None) -> datetime | None:
    match = re.search(r"(-?\d{10,})", raw or "")
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=UTC)


def cafef_research(symbol: str, start_date: str, end_date: str, limit: int = 5) -> list[Headline]:
    """Brokers' research notes on ``symbol`` published in the window, newest first."""
    payload = http.get_json(
        CAFEF_REPORTS,
        vendor="CafeF",
        params={"Symbol": symbol, "PageIndex": 1, "PageSize": 20},
        headers={"Referer": "https://cafef.vn/"},
    )
    window_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=vn_rules.VN_TZ)
    window_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=vn_rules.VN_TZ)
    items: list[Headline] = []
    for row in (payload or {}).get("Data") or []:
        published = _asp_date(row.get("DateDeploy"))
        if published is None or not (window_start <= published < window_end):
            continue
        body = re.sub(r"\s+", " ", str(row.get("Body") or "")).strip()
        items.append(Headline(
            title=str(row.get("Title") or "").strip(),
            source=str(row.get("ResourceName") or row.get("ResourceCode") or "CafeF").strip(),
            published=published,
            summary=body[:600],
            confirmed=True,
        ))
    items.sort(key=lambda h: h.published, reverse=True)
    return items[:limit]


def cafef_company_news(symbol: str, start_date: str, end_date: str, limit: int = 10) -> list[Headline]:
    """CafeF articles tagged to ``symbol`` and published in the window, newest first."""
    window_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=vn_rules.VN_TZ)
    window_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=vn_rules.VN_TZ)
    items: list[Headline] = []
    for page in range(1, CAFEF_MAX_PAGES + 1):
        text = http.get_text(
            CAFEF_TICKER_NEWS, vendor="CafeF", headers={"Referer": "https://cafef.vn/"},
            params={"symbol": symbol, "floorID": 0, "configID": 0, "PageIndex": page, "PageSize": 20, "Type": 2},
        )
        rows = _CAFEF_ROW.findall(text)
        if not rows:
            break
        oldest = None
        for stamp, href, title in rows:
            try:
                published = datetime.strptime(stamp, "%d/%m/%Y %H:%M").replace(tzinfo=vn_rules.VN_TZ)
            except ValueError:
                continue
            oldest = published if oldest is None else min(oldest, published)
            if not (window_start <= published < window_end):
                continue
            title = html.unescape(title).strip()
            if any(k in title.lower() for k in NOISE_KEYWORDS):
                continue
            url = href if href.startswith("http") else f"https://cafef.vn{href.split('?')[0]}"
            items.append(Headline(title=title, source="CafeF", published=published.astimezone(UTC),
                                  url=url, confirmed=True))
        if oldest is not None and oldest < window_start:
            break                      # the window is behind us
    items.sort(key=lambda h: h.published, reverse=True)
    return items[:limit]
