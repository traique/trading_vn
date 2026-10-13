"""The Botkeep app: web page, Telegram commands, the queue and the daily run.

No network and no LLM: the engine and the graph are stubbed; Telegram calls
are recorded instead of sent.
"""

from types import SimpleNamespace

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
    return TestClient(create_app(settings, queue, Watchlist(settings)))


@pytest.mark.unit
def test_the_home_page_lists_jobs_watchlist_and_providers(client):
    page = client.get("/").text
    assert "TradingAgents VN" in page and "FPT, HPG" in page
    assert "Nhà cung cấp AI" in page


@pytest.mark.unit
def test_a_web_request_queues_a_job_and_shows_it(client, queue):
    response = client.post("/analyze", data={"ticker": "fpt", "date": "2026-10-13"}, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[1]
    queue._process(queue.get(job_id))
    page = client.get(f"/jobs/{job_id}").text
    assert "FPT - NO_TRADE" in page and "Mở báo cáo đầy đủ" in page
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
