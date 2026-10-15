"""The Botkeep app: web page, Telegram commands, the queue and the daily run.

No network and no LLM: the engine and the graph are stubbed; Telegram calls
are recorded instead of sent.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vnbot import jobs as jobs_module, service, settings as settings_module
from vnbot.jobs import Job, JobQueue, prune_cache
from vnbot.service import DailyScheduler, Watchlist, check_request
from vnbot.telegram_bot import TelegramBot, chunks, plain
from vnbot.web import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_IDS", "ALLOWED_USER_ID", "WEB_PASSWORD",
                 "WATCHLIST", "PUBLIC_URL", "TELEGRAM_REPORT_CHAT_IDS", "TRADINGAGENTS_RESULTS_DIR",
                 "TRADINGAGENTS_CACHE_DIR", "TRADINGAGENTS_MEMORY_LOG_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WATCHLIST", "fpt, HPG")
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "111,222")
    return settings_module.load()


@pytest.fixture
def engine_stub(monkeypatch):
    from tradingagents.quant import engine

    calls = []

    def analyze(ticker, date, portfolio=None):
        calls.append((ticker, date))
        return SimpleNamespace(action="NO_TRADE", deterministic_summary=f"{ticker} - NO_TRADE")

    monkeypatch.setattr(engine, "analyze", analyze)
    return calls


class _Graph:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def propagate(self, ticker, date):
        return {"final_trade_decision": "**Rating**: NO_TRADE\n\nĐứng ngoài.", "guard_notes": ""}, "NO_TRADE"

    def save_reports(self, state, ticker, save_path):
        save_path.mkdir(parents=True, exist_ok=True)
        (save_path / "complete_report.html").write_text("<h1>báo cáo</h1>", encoding="utf-8")
        return save_path / "complete_report.md"


@pytest.fixture
def queue(settings, engine_stub, tmp_path):
    q = JobQueue(settings)
    q._graph = _Graph(tmp_path)
    return q


@pytest.mark.unit
def test_settings_place_the_framework_files_under_the_data_dir(settings):
    import os

    assert os.environ["TRADINGAGENTS_CACHE_DIR"].startswith(str(settings.data_dir))
    assert settings.telegram_allowed_ids == {111, 222}
    assert settings.watchlist_default == ("FPT", "HPG")


@pytest.mark.unit
def test_the_graph_config_uses_the_provider_chain(settings):
    config = settings_module.graph_config(settings)
    assert config["llm_provider"] == "router9_chain"
    assert config["output_language"] == "Vietnamese"
    assert not config["checkpoint_enabled"]


@pytest.mark.unit
def test_requests_are_checked_before_queueing(monkeypatch):
    monkeypatch.setattr(service, "today_vn", lambda: "2026-10-13")
    assert check_request("fpt.vn", "") == ("FPT", "2026-10-13")
    for bad in ("", "../x", "VNINDEX"):
        with pytest.raises(ValueError):
            check_request(bad)
    with pytest.raises(ValueError, match="tương lai"):
        check_request("FPT", "2026-10-14")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        check_request("FPT", "13/10/2026")


@pytest.mark.unit
def test_a_job_sends_the_system_decision_first_then_the_report(queue):
    events = []
    queue.add_listener(lambda job, event: events.append((event, job.status, job.action)))
    job = queue.submit("FPT", "2026-10-13", "telegram", chat_id=111)

    queue._process(job)

    assert [e[0] for e in events] == ["quick", "done"]
    assert job.quick_summary == "FPT - NO_TRADE" and job.status == "done"
    assert job.html_path is not None and job.html_path.exists()


@pytest.mark.unit
def test_a_restart_marks_unfinished_jobs_failed(settings, engine_stub):
    q = JobQueue(settings)
    job = q.submit("FPT", "2026-10-13", "web")
    again = JobQueue(settings)
    assert again.get(job.id).status == "failed"
    assert "khởi động lại" in again.get(job.id).error


@pytest.mark.unit
def test_old_cache_files_are_pruned(settings):
    import os
    import time

    cache = settings.data_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    old, new = cache / "OLD-dnse-ohlcv.csv", cache / "NEW-dnse-ohlcv.csv"
    old.write_text("x"), new.write_text("x")
    week_ago = time.time() - 7 * 86400
    os.utime(old, (week_ago, week_ago))
    assert prune_cache(settings) == 1 and new.exists() and not old.exists()


@pytest.mark.unit
def test_old_report_folders_are_pruned(settings, queue, monkeypatch):
    object.__setattr__(queue.settings, "keep_reports", 2)
    for _ in range(4):
        queue._process(queue.submit("FPT", "2026-10-13", "web"))
    reports = settings.data_dir / "results" / "reports"
    assert len(list(reports.iterdir())) == 2


# ─── Web ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(settings, queue, monkeypatch):
    monkeypatch.setattr(service, "today_vn", lambda: "2026-10-13")
    object.__setattr__(settings, "web_password", "s3cret")
    client = TestClient(create_app(settings, queue, Watchlist(settings)))
    client.auth = ("u", "s3cret")
    return client


@pytest.mark.unit
def test_the_home_page_lists_jobs_watchlist_and_providers(client):
    page = client.get("/").text
    assert "TradingAgents VN" in page and '<span class="chip">HPG</span>' in page
    assert "Nhà cung cấp AI" in page


@pytest.mark.unit
def test_a_web_request_queues_a_job_and_shows_it(client, queue):
    response = client.post("/analyze", data={"ticker": "fpt", "date": "2026-10-13"}, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[1]
    queue._process(queue.get(job_id))
    page = client.get(f"/jobs/{job_id}").text
    assert "FPT - NO_TRADE" in page and "Báo cáo đầy đủ" in page
    assert "báo cáo" in client.get(f"/reports/{job_id}").text


@pytest.mark.unit
def test_a_bad_ticker_returns_to_the_form_with_the_reason(client):
    response = client.post("/analyze", data={"ticker": "VNINDEX"}, follow_redirects=True)
    assert "là chỉ số" in response.text


@pytest.mark.unit
def test_the_watchlist_is_saved_canonical(client, settings):
    client.post("/watchlist", data={"codes": "vcb, ssi.vn"})
    assert Watchlist(settings).get() == ["VCB", "SSI"]


@pytest.mark.unit
def test_a_password_protects_every_page_but_health(settings, queue, monkeypatch):
    object.__setattr__(settings, "web_password", "s3cret")
    client = TestClient(create_app(settings, queue, Watchlist(settings)))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("x", "wrong")).status_code == 401
    assert client.get("/", auth=("x", "s3cret")).status_code == 200
    assert client.get("/health").status_code == 200


# ─── Telegram ────────────────────────────────────────────────────────────────


@pytest.fixture
def bot(settings, queue):
    object.__setattr__(settings, "telegram_token", "t")
    sent = []
    bot = TelegramBot(settings, queue, Watchlist(settings))
    bot._call = lambda method, http_timeout=30, **params: sent.append((method, params)) or {}
    bot.sent = sent
    return bot


def _msg(text, user=111):
    return {"chat": {"id": user}, "from": {"id": user}, "text": text}


def _texts(bot):
    return [p.get("text", "") for m, p in bot.sent if m == "sendMessage"]


@pytest.mark.unit
def test_an_unknown_user_is_told_their_id(bot):
    bot._handle(_msg("/phantich FPT", user=999))
    assert "999" in _texts(bot)[0] and "TELEGRAM_ALLOWED_IDS" in _texts(bot)[0]


@pytest.mark.unit
def test_phantich_queues_and_the_results_reach_the_chat(bot, queue):
    bot._handle(_msg("/phantich fpt 2026-10-13"))
    job = queue.recent(1)[0]
    assert job.ticker == "FPT" and job.chat_id == 111
    queue._process(job)
    texts = _texts(bot)
    assert "Đã nhận FPT" in texts[0]
    assert "Quyết định hệ thống định lượng" in texts[1]
    assert texts[2].startswith("✅ FPT 2026-10-13 - NO_TRADE")
    assert any(m == "sendDocument" for m, _ in bot.sent)


@pytest.mark.unit
def test_nhanh_answers_with_the_engine_only(bot, engine_stub):
    bot._handle(_msg("/nhanh hpg"))
    assert _texts(bot) == ["HPG - NO_TRADE"]


@pytest.mark.unit
def test_theodoi_sets_and_shows_the_watchlist(bot):
    bot._handle(_msg("/theodoi vcb ssi"))
    bot._handle(_msg("/theodoi"))
    assert "VCB, SSI" in _texts(bot)[-1]


@pytest.mark.unit
def test_a_bad_request_gets_a_reason_not_a_crash(bot):
    bot._handle(_msg("/phantich"))
    assert _texts(bot)[0].startswith("⚠️")


@pytest.mark.unit
def test_long_text_is_split_and_markdown_stripped():
    parts = chunks("x" * 9000)
    assert len(parts) == 3 and all(len(p) <= 3900 for p in parts)
    assert plain("**Rating**: BUY\n## Tiêu đề") == "Rating: BUY\nTiêu đề"


# ─── Daily run ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_daily_run_fires_once_per_weekday_after_its_time(settings, queue, monkeypatch):
    from datetime import datetime

    from tradingagents.market import vn_rules

    scheduler = DailyScheduler(settings, queue, Watchlist(settings))
    at = {"now": datetime(2026, 10, 13, 15, 0, tzinfo=vn_rules.VN_TZ)}
    monkeypatch.setattr(vn_rules, "now_vn", lambda: at["now"])
    assert scheduler._due() is None                      # before 15:30
    at["now"] = datetime(2026, 10, 13, 15, 31, tzinfo=vn_rules.VN_TZ)
    assert scheduler._due() == "2026-10-13"
    scheduler._stamp.write_text("2026-10-13", encoding="utf-8")
    assert scheduler._due() is None                      # already ran today
    at["now"] = datetime(2026, 10, 17, 16, 0, tzinfo=vn_rules.VN_TZ)   # Saturday
    assert scheduler._due() is None


@pytest.mark.unit
def test_daily_jobs_report_to_the_configured_chats(bot, queue):
    job = Job(id="j", ticker="FPT", date="2026-10-13", source="daily", status="done",
              action="NO_TRADE", final_decision="x")
    bot.on_job_event(job, "done")
    chats = {p["chat_id"] for m, p in bot.sent if m == "sendMessage"}
    assert chats == {111, 222}


@pytest.mark.unit
def test_jobs_module_keeps_a_bounded_history(settings, engine_stub, monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_JOBS_KEPT", 3)
    q = JobQueue(settings)
    for _ in range(5):
        job = q.submit("FPT", "2026-10-13", "web")
        job.status = "done"
    q.submit("FPT", "2026-10-13", "web")
    assert len(q._jobs) <= 4


# ─── Hardening: locked web, CSRF, queue limits, holidays, Telegram flood ─────


@pytest.mark.unit
def test_without_a_password_the_web_stays_locked(settings, queue):
    client = TestClient(create_app(settings, queue, Watchlist(settings)))
    response = client.get("/")
    assert response.status_code == 503 and "WEB_PASSWORD" in response.text
    assert client.post("/analyze", data={"ticker": "FPT"}).status_code == 503
    assert client.get("/health").status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.unit
def test_a_form_posted_from_another_site_is_refused(client, queue):
    evil = client.post("/analyze", data={"ticker": "FPT", "date": "2026-10-13"},
                       headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert evil.status_code == 403
    cross = client.post("/analyze", data={"ticker": "FPT", "date": "2026-10-13"},
                        headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert cross.status_code == 403
    assert queue.recent() == []
    ok = client.post("/analyze", data={"ticker": "FPT", "date": "2026-10-13"},
                     headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303


@pytest.mark.unit
def test_the_same_ticker_and_date_join_the_waiting_job(queue):
    first = queue.submit("FPT", "2026-10-13", "web")
    again = queue.submit("FPT", "2026-10-13", "telegram", chat_id=111)
    daily = queue.submit("FPT", "2026-10-13", "daily")
    assert first is again is daily
    assert first.chat_ids == [111] and first.sources == ["web", "telegram", "daily"]
    assert len(queue.recent()) == 1
    first.status = "done"
    assert queue.submit("FPT", "2026-10-13", "web") is not first   # a finished one is not reused


@pytest.mark.unit
def test_people_cannot_queue_more_than_max_pending(settings, engine_stub):
    object.__setattr__(settings, "max_pending", 2)
    q = JobQueue(settings)
    q.submit("FPT", "2026-10-13", "web")
    q.submit("HPG", "2026-10-13", "telegram", chat_id=111)
    with pytest.raises(jobs_module.QueueFull):
        q.submit("VCB", "2026-10-13", "web")
    q.submit("VCB", "2026-10-13", "daily")        # the daily run is not capped


@pytest.mark.unit
def test_a_full_queue_is_explained_on_the_web(client, settings, queue):
    object.__setattr__(settings, "max_pending", 1)
    queue.submit("HPG", "2026-10-13", "web")
    page = client.post("/analyze", data={"ticker": "FPT", "date": "2026-10-13"}, follow_redirects=True).text
    assert "Hàng đợi" in page


@pytest.mark.unit
def test_a_merged_job_reaches_the_asker_and_the_report_chats(bot, queue):
    object.__setattr__(bot.settings, "telegram_report_chat_ids", frozenset({999}))
    job = queue.submit("FPT", "2026-10-13", "telegram", chat_id=111)
    queue.submit("FPT", "2026-10-13", "daily")
    bot.on_job_event(job, "quick")
    assert {p["chat_id"] for m, p in bot.sent if m == "sendMessage"} == {111, 999}


@pytest.mark.unit
def test_report_time_is_normalised(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAILY_REPORT_TIME", "9:05")
    assert settings_module.load().daily_report_time == "09:05"
    monkeypatch.setenv("DAILY_REPORT_TIME", "nonsense")
    assert settings_module.load().daily_report_time == "15:30"


@pytest.mark.unit
def test_the_daily_run_skips_a_weekday_holiday(settings, queue, monkeypatch):
    import pandas as pd

    from tradingagents.dataflows import ohlcv
    from tradingagents.market import vn_rules

    monkeypatch.setattr(vn_rules, "is_market_hours", lambda at=None: False)
    bars = {"days": ["2026-09-01"]}
    monkeypatch.setattr(ohlcv, "load_ohlcv", lambda s, d: pd.DataFrame({"Date": pd.to_datetime(bars["days"])}))
    assert DailyScheduler._is_trading_day("2026-09-02") is False      # Quốc khánh
    bars["days"] = ["2026-09-01", "2026-09-03"]
    assert DailyScheduler._is_trading_day("2026-09-03") is True


@pytest.mark.unit
def test_telegram_flood_control_is_waited_out(settings, queue, monkeypatch):
    from vnbot import telegram_bot

    object.__setattr__(settings, "telegram_token", "t")
    bot = TelegramBot(settings, queue, Watchlist(settings))
    replies = [
        {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 1}},
        {"ok": True, "result": {"message_id": 1}},
    ]
    posts = []
    bot._session = SimpleNamespace(post=lambda url, **kw: posts.append(kw) or SimpleNamespace(
        status_code=200, json=lambda: replies[len(posts) - 1]))
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    assert bot._call("sendMessage", chat_id=1, text="x") == {"message_id": 1}
    assert len(posts) == 2


# ─── Trading bot ─────────────────────────────────────────────────────────────


def _result(symbol, action="BUY", price=50_000.0):
    from tradingagents.quant.engine import QuantResult

    plan = action == "BUY"
    return QuantResult(
        symbol=symbol, as_of_date="2026-10-13", last_bar_date="2026-10-13", price=price, exchange="HOSE",
        action=action, confidence=0.8, setup_type="pullback", market_regime="risk_on", risk_level="low",
        data_quality="ok", reasons=[], stop_price=47_000.0 if plan else None,
        target_price=55_000.0 if plan else None, rr_ratio=1.6 if plan else None, invalidation_reason=None,
        entry_low=49_500.0 if plan else None, entry_high=50_200.0 if plan else None,
        target2=58_000.0 if plan else None, position_size_pct=10.0 if plan else None,
    )


@pytest.fixture
def trader(settings, monkeypatch):
    from tradingagents.dataflows import ohlcv
    from tradingagents.quant import engine
    from vnbot.trader import TradingBot

    days = {"2026-10-13": (50_000, 50_300, 49_800, 50_100), "2026-10-14": (50_000, 50_500, 49_900, 50_400)}

    def load(symbol, as_of, fill_gaps=True, sources=None):
        rows = [(d, *v) for d, v in days.items() if d <= as_of]
        return pd.DataFrame([{"Date": pd.Timestamp(d), "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1e6}
                             for d, o, h, lo, c in rows])

    monkeypatch.setattr(ohlcv, "load_ohlcv", load)
    from tradingagents.dataflows import money_flows

    monkeypatch.setattr(money_flows, "index_members", lambda group="VN30": ["FPT", "HPG"])
    decisions = {"FPT": "BUY", "HPG": "WATCH"}
    monkeypatch.setattr(engine, "analyze", lambda s, d, portfolio=None, params=None: _result(s, decisions.get(s, "NO_TRADE")))
    # A fixed clock (the T+2 afternoon of the 2026-10-14 buy) and no network: the
    # tests must not depend on the calendar date (fills dated in the future are refused).
    import vnbot.trader as trader_mod
    from tradingagents.dataflows.vendors import vci

    frozen = datetime(2026, 10, 16, 15, 30, tzinfo=timezone(timedelta(hours=7)))
    monkeypatch.setattr(trader_mod, "_now_vn", lambda: frozen)
    monkeypatch.setattr(service, "today_vn", lambda: "2026-10-16")
    monkeypatch.setattr(vci, "fetch_listing", lambda: [{"symbol": "FPT", "board": "HOSE"},
                                                       {"symbol": "HPG", "board": "HOSE"}])
    bot = TradingBot(settings, Watchlist(settings))
    monkeypatch.setattr(bot, "universe", lambda: ["FPT", "HPG"])
    bot.decisions = decisions
    return bot


@pytest.mark.unit
def test_the_bot_places_tomorrows_order_and_fills_it_the_next_session(trader):
    import pandas as pd  # noqa: F401

    report = trader.run_day("2026-10-13")
    assert "Lệnh cho phiên tới" in report and "BUY FPT" in report
    paper = trader.paper()
    assert paper.orders[0].symbol == "FPT" and paper.orders[0].qty % 100 == 0
    report = trader.run_day("2026-10-14")
    assert "Khớp hôm nay" in report and "FPT" in trader.paper().positions
    assert "FPT" in trader.portfolio_text()


@pytest.mark.unit
def test_signal_mode_suggests_real_orders_and_records_reported_fills(trader, settings):
    object.__setattr__(settings, "trade_mode", "signal")
    object.__setattr__(settings, "signal_strategy", "engine")
    report = trader.run_day("2026-10-13")
    assert "TÍN HIỆU CHO TÀI KHOẢN THẬT" in report and "/von" in report
    trader.set_capital(200_000_000)
    report = trader.run_day("2026-10-14")
    assert "MUA FPT" in report
    text = trader.record_fill("BUY", "FPT", 1000, 50_100, "2026-10-14")
    assert "stop 47.000" in text
    real = trader.real()
    assert real.positions["FPT"].qty == 1000 and real.cash < 200_000_000 - 50_100_000
    with pytest.raises(ValueError, match="đủ FPT để bán"):   # the oversell guard, not the date check
        trader.record_fill("SELL", "FPT", 5000, 51_000, "2026-10-16")
    assert "lãi/lỗ" in trader.record_fill("SELL", "FPT", 1000, 51_000, "2026-10-16")


@pytest.mark.unit
def test_trading_commands_on_telegram(bot, trader, settings):
    bot.trader = trader
    bot._handle(_msg("/danhmuc"))
    bot._handle(_msg("/lenh"))
    bot._handle(_msg("/hieuchinh"))
    bot._handle(_msg("/khop mua FPT 100 50.100"))           # paper mode: refused with a reason
    texts = _texts(bot)
    assert "Paper" in texts[0] and "Động lượng" in texts[0] and "không có lệnh chờ" in texts[1]
    assert "mặc định" in texts[2]
    assert "chế độ tín hiệu" in texts[3]
    object.__setattr__(settings, "trade_mode", "signal")
    bot._handle(_msg("/von 100.000.000"))
    bot._handle(_msg("/khop mua FPT 100 50.100"))
    assert "100.000.000" in _texts(bot)[-2] and "Đã ghi MUA FPT" in _texts(bot)[-1]


@pytest.mark.unit
def test_the_bot_page_shows_the_book_and_the_thresholds(settings, queue, trader):
    object.__setattr__(settings, "web_password", "s3cret")
    trader.run_day("2026-10-13")
    client = TestClient(create_app(settings, queue, Watchlist(settings), trader=trader))
    client.auth = ("u", "s3cret")
    page = client.get("/bot").text
    assert "Quant Engine" in page and "Động lượng VN30" in page and "So sánh hai chiến lược" in page and "FPT" in page
    assert "confidence_buy_min" in page and "mom_lookback" in page
    assert "/bot" in client.get("/").text


@pytest.mark.unit
def test_the_bot_page_and_help_show_when_the_bot_really_runs(settings, queue, trader):
    # Regression: the page showed DAILY_REPORT_TIME (the watchlist run), not TRADE_TIME.
    from vnbot.telegram_bot import TelegramBot

    object.__setattr__(settings, "web_password", "s3cret")
    object.__setattr__(settings, "daily_report_time", "08:30")
    object.__setattr__(settings, "trade_time", "16:00")
    client = TestClient(create_app(settings, queue, Watchlist(settings), trader=trader))
    client.auth = ("u", "s3cret")
    page = client.get("/bot").text
    assert "tự chạy lúc 16:00" in page and "tự chạy 08:30" not in page
    assert "lúc 16:00, hoặc bấm" in page and "15:30" not in page
    assert "không đợi 16:00" in TelegramBot(settings, queue, Watchlist(settings))._help()


@pytest.mark.unit
def test_the_daily_run_trades_then_calibrates(settings, queue, monkeypatch):
    calls, notes = [], []

    class Bot:
        def run_day(self, day):
            calls.append(("trade", day))
            return "report"

        def calibrate(self, day):
            calls.append(("calibrate", day))
            raise RuntimeError("no history")

    scheduler = DailyScheduler(settings, queue, Watchlist(settings), bot=Bot(), notify=notes.append)
    scheduler._trade("2026-10-13")
    scheduler._calibrate("2026-10-13")
    assert calls == [("trade", "2026-10-13"), ("calibrate", "2026-10-13")]
    assert notes[0] == "report" and "không chạy được" in notes[1]
    object.__setattr__(settings, "trade_mode", "off")
    scheduler._trade("2026-10-14")
    assert len(calls) == 2


@pytest.mark.unit
def test_the_momentum_book_rotates_into_the_strongest_stocks(settings, monkeypatch):
    from tradingagents.dataflows import money_flows, ohlcv
    from tradingagents.quant import engine, params as qp
    from vnbot.trader import TradingBot

    days = pd.bdate_range(end="2026-10-14", periods=130)
    paths = {"VNINDEX": [1000 + i for i in range(130)], "AAA": [10_000 + 100 * i for i in range(130)],
             "BBB": [20_000 - 50 * i for i in range(130)], "CCC": [15_000 + 30 * i for i in range(130)]}

    def load(symbol, as_of, fill_gaps=True, sources=None):
        rows = [(d, c) for d, c in zip(days, paths[symbol], strict=True) if d <= pd.Timestamp(as_of)]
        return pd.DataFrame([{"Date": d, "Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6}
                             for d, c in rows])

    monkeypatch.setattr(ohlcv, "load_ohlcv", load)
    monkeypatch.setattr(money_flows, "index_members", lambda group="VN30": ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(engine, "analyze", lambda s, d, portfolio=None, params=None: _result(s, "NO_TRADE"))
    monkeypatch.setattr(qp, "active", lambda: qp.replace(qp.DEFAULT, mom_top_k=2))
    bot = TradingBot(settings, Watchlist(settings))
    monkeypatch.setattr(bot, "universe", lambda: ["AAA"])
    report = bot.run_day("2026-10-13")
    assert "BOT ĐỘNG LƯỢNG" in report and "So sánh" in report
    book = bot.momentum_book()
    assert book.meta["mom_basket"] == ["AAA", "CCC"]            # BBB fell: never in the basket
    assert {o.symbol for o in book.orders} == {"AAA", "CCC"}
    object.__setattr__(settings, "trade_mode", "signal")
    bot.set_capital(100_000_000)
    report = bot.run_day("2026-10-14")
    assert set(bot.momentum_book().positions) == {"AAA", "CCC"}
    assert "TÍN HIỆU THẬT - ĐỘNG LƯỢNG" in report and "MUA AAA" in report


@pytest.mark.unit
def test_a_session_runs_once_and_chay_runs_the_last_closed_one(trader, monkeypatch):
    first = trader.run_day("2026-10-13")
    orders = len(trader.paper().orders)
    assert trader.run_day("2026-10-13") == first and len(trader.paper().orders) == orders   # not settled twice
    monkeypatch.setattr(trader, "session_day", lambda: "2026-10-13")
    assert "đã chạy rồi" in trader.start_run(notify=lambda t: None)
    sent = []
    monkeypatch.setattr(trader, "session_day", lambda: "2026-10-14")
    assert "Đang chạy phiên 2026-10-14" in trader.start_run(notify=sent.append)
    for _ in range(200):
        if sent:
            break
        __import__("time").sleep(0.01)
    assert sent and "2026-10-14" in sent[0] and not trader.running


@pytest.mark.unit
def test_pages_carry_the_theme_switch_and_the_bot_page_runs_a_session(settings, queue, trader, monkeypatch):
    object.__setattr__(settings, "web_password", "s3cret")
    client = TestClient(create_app(settings, queue, Watchlist(settings), trader=trader))
    client.auth = ("u", "s3cret")
    home = client.get("/").text
    assert 'id="theme-toggle"' in home and "prefers-color-scheme:dark" in home and "data-theme=dark" in home
    assert "Biểu đồ NAV xuất hiện sau 2 phiên" in client.get("/bot").text
    trader.run_day("2026-10-13")
    trader.run_day("2026-10-14")
    page = client.get("/bot").text
    assert "<polyline" in page and "VNINDEX" in page and "Chạy phiên ngay" in page
    monkeypatch.setattr(trader, "session_day", lambda: "2026-10-14")
    response = client.post("/bot/run", follow_redirects=False)
    assert response.status_code == 303 and "/bot?msg=" in response.headers["location"]
    assert "đã chạy rồi" in client.get(response.headers["location"]).text
    evil = client.post("/bot/run", headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert evil.status_code == 403
