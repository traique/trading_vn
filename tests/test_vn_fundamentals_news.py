"""VCI fundamentals and the Vietnamese news feeds are served point-in-time.

A statement exists for a run only from the day it was published; news and
research notes only inside the run's window. No network: VCI, CafeF and Google
News answers are canned in the shapes the live endpoints return.
"""

from datetime import UTC, datetime

import pytest

from tradingagents.dataflows import news, vn_fundamentals
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import vci, vn_news

QUARTERS = [
    {"yearReport": 2025, "lengthReport": 4, "publicDate": "2026-01-25T00:00:00", "isa3": 16e12, "isa20": 2.4e12},
    {"yearReport": 2026, "lengthReport": 1, "publicDate": "2026-04-22T00:00:00", "isa3": 12.5e12, "isa20": 2.5e12},
    {"yearReport": 2026, "lengthReport": 2, "publicDate": "2026-08-22T00:00:00", "isa3": 13.8e12, "isa20": 2.57e12},
]
RATIOS = [
    {"yearReport": 2025, "quarter": 4, "pe": 18.0, "pb": 4.0, "roe": 0.27},
    {"yearReport": 2026, "quarter": 1, "pe": 14.0, "pb": 3.2, "roe": 0.26},
    {"yearReport": 2026, "quarter": 2, "pe": 10.7, "pb": 2.7, "roe": 0.265},
]


@pytest.fixture
def vci_data(monkeypatch):
    monkeypatch.setattr(vci, "fetch_statement", lambda symbol, section: {"quarters": QUARTERS, "years": []})
    monkeypatch.setattr(vci, "fetch_ratios", lambda symbol: list(RATIOS))
    monkeypatch.setattr(vci, "fetch_metric_labels", lambda symbol: {
        "INCOME_STATEMENT": [{"field": "isa3", "titleVi": "Doanh thu thuần", "level": 1},
                             {"field": "isa20", "titleVi": "Lãi/(lỗ) thuần sau thuế", "level": 1}],
    })
    details = []
    monkeypatch.setattr(vci, "fetch_details", lambda symbol: details.append(symbol) or {
        "comTypeCode": "CT", "viOrganName": "Công ty Cổ phần FPT", "foreignerPercentage": 0.28,
        "maximumForeignPercentage": 0.49, "targetPrice": 90300, "rating": "BUY",
    })
    monkeypatch.setattr(vn_fundamentals, "_peer_benchmark", lambda *a: (None, 0))
    return details


@pytest.mark.unit
def test_a_statement_is_unseen_before_its_publication_date(vci_data):
    rows = vn_fundamentals.published_statement_rows("FPT", "INCOME_STATEMENT", "2026-08-21")
    assert [(r["yearReport"], r["lengthReport"]) for r in rows] == [(2026, 1), (2025, 4)]
    rows = vn_fundamentals.published_statement_rows("FPT", "INCOME_STATEMENT", "2026-08-22")
    assert rows[0]["lengthReport"] == 2


@pytest.mark.unit
def test_ratios_follow_their_quarter_publication(vci_data):
    assert vn_fundamentals.published_ratio_rows("FPT", "2026-05-01")[0]["pe"] == 14.0
    assert vn_fundamentals.published_ratio_rows("FPT", "2026-09-01")[0]["pe"] == 10.7


@pytest.mark.unit
def test_an_undated_quarter_waits_for_the_filing_deadline():
    row = {"yearReport": 2026, "lengthReport": 2, "publicDate": None}
    assert str(vn_fundamentals.published_on(row)) == "2026-08-14"  # 30/06 + 45 days
    assert str(vn_fundamentals.published_on({"yearReport": 2025}, annual=True)) == "2026-03-31"


@pytest.mark.unit
def test_a_past_run_does_not_see_live_profile_fields(vci_data):
    text = vn_fundamentals.get_fundamentals("FPT", "2026-05-01")
    assert vci_data == []  # details never fetched for a historical run
    assert "Q1/2026" in text and "90.300" not in text and "Bị giữ lại" in text


@pytest.mark.unit
def test_fundamentals_render_vietnamese_numbers(vci_data, monkeypatch):
    monkeypatch.setattr(vn_fundamentals, "is_historical", lambda d: False)
    text = vn_fundamentals.get_fundamentals("FPT", "2026-10-13")
    assert "P/E: 10,70" in text and "ROE: 26,50%" in text
    assert "Giá mục tiêu của VCI: 90.300 VND" in text


@pytest.mark.unit
def test_statement_table_labels_and_publication_dates(vci_data):
    text = vn_fundamentals.get_income_statement("FPT", "quarterly", "2026-10-13")
    assert "Q2/2026 (công bố 22/08/2026)" in text
    assert "| Doanh thu thuần | 13.800,0 |" in text


@pytest.mark.unit
def test_no_statement_published_by_the_date_is_no_data(vci_data):
    with pytest.raises(NoMarketDataError):
        vn_fundamentals.get_income_statement("FPT", "quarterly", "2025-01-01")


@pytest.mark.unit
def test_corporate_events_after_the_run_date_are_hidden(monkeypatch):
    events = [
        {"eventCode": "ISS", "publicDate": "2026-09-16T00:00:00", "exrightDate": "2026-09-21T00:00:00",
         "eventTitleVi": "Cổ phiếu thưởng tỉ lệ 10.0%"},
        {"eventCode": "DDIND", "publicDate": "2026-06-29T00:00:00", "eventTitleVi": "Đăng kí Mua 428,368 FPT"},
    ]
    monkeypatch.setattr(vci, "fetch_events", lambda *a, **k: events)
    text = vn_fundamentals.get_corporate_events("FPT", "2026-07-01")
    assert "Đăng kí Mua" in text and "thưởng" not in text


# ─── News ────────────────────────────────────────────────────────────────────

RSS = """<rss><channel>
<item><title>FPT lãi kỷ lục quý 2 - CafeF</title><source>CafeF</source>
<pubDate>Tue, 13 Oct 2026 03:00:00 GMT</pubDate><link>https://a</link></item>
<item><title>VN-Index giảm nhẹ phiên chiều - VnExpress</title><source>VnExpress</source>
<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate><link>https://b</link></item>
<item><title>Tin tương lai về FPT - X</title><source>X</source>
<pubDate>Thu, 15 Oct 2026 08:00:00 GMT</pubDate><link>https://c</link></item>
<item><title>CFPT2601 chứng quyền tăng - Y</title><source>Y</source>
<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate><link>https://d</link></item>
</channel></rss>"""


@pytest.mark.unit
def test_google_news_keeps_only_the_window_and_drops_warrants(monkeypatch):
    seen = {}
    monkeypatch.setattr(vn_news.http, "get_text", lambda url, **k: seen.update(k) or RSS)
    items = vn_news.google_news('"FPT" cổ phiếu', "2026-10-06", "2026-10-13")
    assert "after:2026-10-06 before:2026-10-14" in seen["params"]["q"]
    assert [i.title for i in items] == ["FPT lãi kỷ lục quý 2", "VN-Index giảm nhẹ phiên chiều"]


@pytest.mark.unit
def test_cafef_research_is_windowed(monkeypatch):
    payload = {"Data": [
        {"Title": "Lựa chọn của tuần FPT", "DateDeploy": "/Date(1790035200000)/", "ResourceName": "EVS", "Body": "AI"},
        {"Title": "Cũ", "DateDeploy": "/Date(1600000000000)/", "ResourceName": "SSI", "Body": ""},
    ]}
    monkeypatch.setattr(vn_news.http, "get_json", lambda *a, **k: payload)
    notes = vn_news.cafef_research("FPT", "2026-09-01", "2026-10-13")
    assert [n.title for n in notes] == ["Lựa chọn của tuần FPT"] and notes[0].confirmed


@pytest.mark.unit
def test_company_news_marks_items_about_the_ticker(monkeypatch):
    monkeypatch.setattr(vci, "fetch_news", lambda *a, **k: [
        {"newsTitle": "FPT: Nghị quyết HĐQT", "publicDate": "2026-10-08T16:19:09"}])
    monkeypatch.setattr(vn_news, "cafef_research", lambda *a, **k: [])
    monkeypatch.setattr(vn_news.http, "get_text", lambda url, **k: RSS)
    text = news.get_news("FPT", "2026-10-06", "2026-10-13")
    assert "### ✅ FPT: Nghị quyết HĐQT" in text
    assert "### ✅ FPT lãi kỷ lục quý 2" in text
    assert "### ⚠️ VN-Index giảm nhẹ phiên chiều" in text
    assert "tương lai" not in text


@pytest.mark.unit
def test_every_news_feed_down_is_unavailable(monkeypatch):
    def down(*a, **k):
        raise VendorUnavailableError("down")

    monkeypatch.setattr(vci, "fetch_news", down)
    monkeypatch.setattr(vn_news, "cafef_research", down)
    monkeypatch.setattr(vn_news, "google_news", down)
    with pytest.raises(VendorUnavailableError):
        news.get_news("FPT", "2026-10-06", "2026-10-13")


@pytest.mark.unit
def test_keyword_sentiment_respects_negation():
    assert news.sentiment_score("FPT lãi kỷ lục") > 0
    assert news.sentiment_score("Khối ngoại bán ròng FPT") < 0
    assert news.sentiment_score("FPT không giảm") > 0


@pytest.mark.unit
def test_headline_dates_are_utc_aware():
    item = vn_news.Headline("t", "s", datetime(2026, 10, 13, tzinfo=UTC))
    assert item.published.tzinfo is not None
