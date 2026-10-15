"""Regression tests for the Telegram bot and web page fixes.

Telegram: edited messages never re-run a command, a batch is never handled
twice (not after an error, not after a restart), the bot token never reaches a
log or a chat, and a burst of messages runs on a bounded set of threads.
Web: times in Vietnam time, a wrong-password lockout, error pages without
exception details, bounded form input.

No network: the Bot API and the engine are faked.
"""

import logging
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from tests.test_vnbot import engine_stub, queue, settings  # noqa: F401 - shared fixtures
from vnbot import service, telegram_bot
from vnbot.service import Watchlist
from vnbot.telegram_bot import TelegramBot, TelegramNetworkError, _amount, redact
from vnbot.web import _when, create_app

TOKEN = "123456789:AAHxYz0123456789abcdefghijklmnopqrstu"


class _Stop(BaseException):
    """Ends the endless polling loop from inside a fake API call."""


def _msg(text, user=111, message_id=1):
    return {"message_id": message_id, "chat": {"id": user}, "from": {"id": user}, "text": text}


@pytest.fixture
def tg(settings, queue, monkeypatch):  # noqa: F811
    object.__setattr__(settings, "telegram_token", TOKEN)
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    bot = TelegramBot(settings, queue, Watchlist(settings))
    bot.sent = []
    bot._call = lambda method, http_timeout=30, **params: bot.sent.append((method, params)) or {}
    return bot


def _texts(bot):
    return [p.get("text", "") for m, p in bot.sent if m == "sendMessage"]


class _FakeTrader:
    def __init__(self):
        self.fills, self.capital = [], []

    def record_fill(self, side, symbol, qty, price, day, **kwargs):
        self.fills.append((side, symbol, qty, price, day))
        return f"✅ Đã ghi {side} {symbol}"

    def set_capital(self, cash, **kwargs):
        self.capital.append(cash)
        return f"✅ Vốn {cash:.0f}"


@pytest.fixture
def signal_bot(tg, settings, monkeypatch):  # noqa: F811
    object.__setattr__(settings, "trade_mode", "signal")
    monkeypatch.setattr(service, "today_vn", lambda: "2026-10-15")   # a Thursday
    tg.trader = _FakeTrader()
    return tg


# ─── 1. edited messages, offsets, restarts ───────────────────────────────────


@pytest.mark.unit
def test_polling_asks_only_for_new_messages_and_ignores_edits(tg):
    calls, dispatched = [], []
    batch = [
        {"update_id": 10, "message": _msg("/khop mua FPT 100 56300")},
        {"update_id": 11, "edited_message": _msg("/khop mua FPT 100 56300")},
    ]

    def fake_call(method, http_timeout=30, **params):
        calls.append(params)
        if len(calls) == 1:
            return batch
        raise _Stop

    tg._call = fake_call
    tg._dispatch = dispatched.append
    with pytest.raises(_Stop):
        tg._poll()
    assert "edited_message" not in calls[0]["allowed_updates"]
    assert [m["text"] for m in dispatched] == ["/khop mua FPT 100 56300"]   # the edit is not a command
    assert calls[1]["offset"] == 12                                       # the whole batch confirmed


@pytest.mark.unit
def test_editing_khop_does_not_record_the_fill_twice(signal_bot):
    _, messages = signal_bot._accept([{"update_id": 1, "message": _msg("/khop mua FPT 100 56300")}])
    for message in messages:
        signal_bot._handle(message)
    _, messages = signal_bot._accept([{"update_id": 2, "edited_message": _msg("/khop mua FPT 100 56.300")}])
    for message in messages:
        signal_bot._handle(message)
    assert signal_bot.trader.fills == [("BUY", "FPT", 100, 56300.0, "2026-10-15")]


@pytest.mark.unit
def test_an_error_while_queueing_does_not_replay_the_batch(tg):
    calls, handled = [], []
    batch = [{"update_id": 5, "message": _msg("/von 1")}, {"update_id": 6, "message": _msg("/von 2")}]

    def fake_call(method, http_timeout=30, **params):
        calls.append(params)
        if len(calls) == 1:
            return batch
        if len(calls) == 2:
            return batch            # Telegram would not, but a replay must still be refused
        raise _Stop

    def dispatch(message):
        handled.append(message["text"])
        raise RuntimeError("can't start new thread")

    tg._call, tg._dispatch = fake_call, dispatch
    with pytest.raises(_Stop):
        tg._poll()
    assert calls[1]["offset"] == 7 and calls[2]["offset"] == 7
    assert handled == ["/von 1", "/von 2"]


@pytest.mark.unit
def test_a_restart_does_not_rerun_updates_already_handed_out(tg, settings, queue):  # noqa: F811
    batch = [{"update_id": 42, "message": _msg("/von 100000000")}]
    offset, messages = tg._accept(batch)
    assert offset == 43 and len(messages) == 1
    # The process dies before the next getUpdates confirms the batch; Telegram
    # hands it out again to the new process.
    again = TelegramBot(settings, queue, Watchlist(settings))
    offset, messages = again._accept(batch)
    assert offset == 43 and messages == []
    _, messages = again._accept([{"update_id": 43, "message": _msg("/danhmuc")}])
    assert [m["text"] for m in messages] == ["/danhmuc"]


@pytest.mark.unit
def test_a_malformed_update_is_skipped_not_fatal(tg):
    offset, messages = tg._accept([{"message": _msg("/id")}, {"update_id": 3, "message": {"text": "/id"}},
                                   {"update_id": 4, "message": _msg("/id")}])
    assert offset == 5 and [m["text"] for m in messages] == ["/id"]


# ─── 2. the token never reaches a log or a chat ──────────────────────────────


@pytest.mark.unit
def test_network_errors_lose_the_token(settings, queue):  # noqa: F811
    object.__setattr__(settings, "telegram_token", TOKEN)
    bot = TelegramBot(settings, queue, Watchlist(settings))

    def post(url, **kwargs):
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")

    bot._session = SimpleNamespace(post=post)
    with pytest.raises(TelegramNetworkError) as info:
        bot._call("getUpdates")
    assert TOKEN not in str(info.value) and "<TOKEN>" in str(info.value)
    assert info.value.__suppress_context__          # the original (with the URL) is not printed
    assert isinstance(info.value, requests.RequestException)


@pytest.mark.unit
def test_polling_logs_never_contain_the_token(tg, caplog):
    calls = []

    def fake_call(method, http_timeout=30, **params):
        calls.append(method)
        if len(calls) == 1:
            raise requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/getUpdates")
        if len(calls) == 2:
            raise ValueError(f"boom https://api.telegram.org/bot{TOKEN}/getUpdates")
        raise _Stop

    tg._call = fake_call
    with caplog.at_level(logging.DEBUG), pytest.raises(_Stop):
        tg._poll()
    assert "Telegram polling" in caplog.text
    assert TOKEN not in caplog.text
    assert all(TOKEN not in (r.exc_text or "") for r in caplog.records)


@pytest.mark.unit
def test_logger_exception_traceback_is_redacted(tg, caplog):
    with caplog.at_level(logging.INFO):
        try:
            raise RuntimeError(f"url /bot{TOKEN}/sendMessage")
        except RuntimeError:
            telegram_bot.logger.exception("send failed for %s", f"bot{TOKEN}")
    assert "send failed" in caplog.text and "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.unit
def test_a_failing_command_does_not_echo_the_token_to_the_chat(tg, monkeypatch):
    def broken():
        raise RuntimeError(f"HTTPSConnectionPool url: /bot{TOKEN}/getMe")

    monkeypatch.setattr(tg, "_status", broken)
    tg._handle(_msg("/trangthai"))
    assert _texts(tg) and TOKEN not in _texts(tg)[0] and _texts(tg)[0].startswith("⚠️ Lỗi")


@pytest.mark.unit
def test_redact_handles_any_bot_token_shape():
    assert redact(f"x /bot{TOKEN}/y") == "x /bot<TOKEN>/y"
    assert redact("price 56:300 at 10:30") == "price 56:300 at 10:30"


# ─── 3. bounded command threads ──────────────────────────────────────────────


@pytest.mark.unit
def test_a_burst_runs_on_a_few_threads_and_the_excess_is_dropped(tg, monkeypatch):
    release, started = threading.Event(), []
    names = set()

    def slow(message):
        names.add(threading.current_thread().name)
        started.append(message["text"])
        release.wait(5)

    monkeypatch.setattr(tg, "_handle", slow)
    before = threading.active_count()
    # Allowed ids only (111, 222): strangers no longer share this budget (see below).
    for i in range(telegram_bot.MAX_QUEUED_COMMANDS + 20):
        tg._dispatch(_msg(f"/nhanh {i}", user=(111, 222)[i % 2]))
    assert tg._queued == telegram_bot.MAX_QUEUED_COMMANDS
    assert threading.active_count() - before <= telegram_bot.COMMAND_LANES
    release.set()
    deadline = time.time() + 5
    while tg._queued and time.time() < deadline:
        time.sleep(0.01)
    assert tg._queued == 0 and len(started) == telegram_bot.MAX_QUEUED_COMMANDS
    assert len(names) <= telegram_bot.COMMAND_LANES


@pytest.mark.unit
def test_a_strangers_flood_cannot_make_the_bot_drop_the_owners_fill(tg, monkeypatch):
    # Regression: unauthorised messages took the shared slots before the id check,
    # so 60 messages from a stranger made the owner's "/khop" silently disappear.
    release, handled = threading.Event(), []

    def slow(message):
        handled.append(message["text"])
        if message["from"]["id"] == 999:
            release.wait(5)

    monkeypatch.setattr(tg, "_handle", slow)
    for i in range(60):
        tg._dispatch(_msg(f"spam {i}", user=999))
    tg._dispatch(_msg("/khop mua FPT 1000 56300", user=111))
    assert tg._queued == 1                     # the owner's command is queued
    release.set()
    deadline = time.time() + 5
    while (tg._queued or tg._strangers_queued) and time.time() < deadline:
        time.sleep(0.01)
    assert "/khop mua FPT 1000 56300" in handled
    assert sum(t.startswith("spam") for t in handled) == 1      # one reply per chat per window


@pytest.mark.unit
def test_a_stranger_still_gets_its_id_once_per_window(tg, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(telegram_bot.time, "monotonic", lambda: clock["now"])
    tg._dispatch(_msg("/id", user=999))
    tg._dispatch(_msg("/id", user=999))
    deadline = time.time() + 5
    while tg._strangers_queued and time.time() < deadline:
        time.sleep(0.01)
    assert _texts(tg) == ["Telegram id của bạn: 999"]
    clock["now"] += telegram_bot.STRANGER_REPLY_SECONDS + 1
    tg._dispatch(_msg("/id", user=999))
    deadline = time.time() + 5
    while tg._strangers_queued and time.time() < deadline:
        time.sleep(0.01)
    assert len(_texts(tg)) == 2


@pytest.mark.unit
def test_the_owner_is_told_when_a_command_is_dropped(tg, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(tg, "_handle", lambda message: release.wait(5))
    for i in range(telegram_bot.MAX_QUEUED_COMMANDS):
        tg._dispatch(_msg(f"/nhanh {i}", user=222))
    tg._dispatch(_msg("/khop mua FPT 1000 56300", user=111))
    release.set()
    deadline = time.time() + 5
    while not _texts(tg) and time.time() < deadline:
        time.sleep(0.01)
    assert any("Bot đang bận" in t and "/khop mua FPT" in t for t in _texts(tg))


@pytest.mark.unit
def test_one_chats_commands_run_in_order(tg, monkeypatch):
    done = []
    monkeypatch.setattr(tg, "_handle", lambda message: done.append(message["text"]))
    for i in range(20):
        tg._dispatch(_msg(f"/von {i}"))
    deadline = time.time() + 5
    while tg._queued and time.time() < deadline:
        time.sleep(0.01)
    assert done == [f"/von {i}" for i in range(20)]


@pytest.mark.unit
def test_a_crashing_handler_frees_its_slot(tg, monkeypatch):
    monkeypatch.setattr(tg, "_handle", lambda message: 1 / 0)
    tg._dispatch(_msg("/id"))
    deadline = time.time() + 5
    while tg._queued and time.time() < deadline:
        time.sleep(0.01)
    assert tg._queued == 0


# ─── /khop and /von input ────────────────────────────────────────────────────


@pytest.mark.unit
def test_amounts_need_whole_thousand_groups():
    assert _amount("56300") == 56300 and _amount("56.300") == 56300 and _amount("100.000.000") == 1e8
    assert _amount("1,000") == 1000
    for bad in ("56.3", "56,30", "1.0000", "", "abc", "-5", ".300"):
        with pytest.raises(ValueError):
            _amount(bad)


@pytest.mark.unit
def test_khop_refuses_shorthand_prices_and_zero_lots(signal_bot):
    signal_bot._handle(_msg("/khop mua FPT 100 56.3"))
    signal_bot._handle(_msg("/khop mua FPT 0 56300"))
    signal_bot._handle(_msg("/khop mua FPT 100 56"))
    assert signal_bot.trader.fills == []
    assert all(t.startswith("⚠️") for t in _texts(signal_bot))


@pytest.mark.unit
def test_khop_takes_the_fill_day_for_the_t2_clock(signal_bot):
    signal_bot._handle(_msg("/khop mua FPT 100 56300 2026-10-13"))
    signal_bot._handle(_msg("/khop ban FPT 100 57.000"))
    assert signal_bot.trader.fills == [("BUY", "FPT", 100, 56300.0, "2026-10-13"),
                                       ("SELL", "FPT", 100, 57000.0, "2026-10-15")]
    signal_bot._handle(_msg("/khop mua FPT 100 56300 2026-10-10"))   # Saturday
    signal_bot._handle(_msg("/khop mua FPT 100 56300 2026-10-16"))   # tomorrow
    assert len(signal_bot.trader.fills) == 2
    assert "cuối tuần" in _texts(signal_bot)[-2] and "tương lai" in _texts(signal_bot)[-1]


@pytest.mark.unit
def test_theodoi_caps_the_list(tg):
    tg._handle(_msg("/theodoi " + ",".join(["FPT"] * 31)))
    assert "Tối đa 30" in _texts(tg)[0]


# ─── 4-5. web ────────────────────────────────────────────────────────────────


@pytest.fixture
def web(settings, queue, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "today_vn", lambda: "2026-10-13")
    object.__setattr__(settings, "web_password", "s3cret")
    app = create_app(settings, queue, Watchlist(settings))
    client = TestClient(app)
    client.auth = ("u", "s3cret")
    return app, client


@pytest.mark.unit
def test_times_on_the_page_are_vietnam_time():
    ts = datetime(2026, 10, 13, 1, 30, tzinfo=UTC).timestamp()
    assert _when(ts) == "13/10 08:30"
    assert _when(None) == ""


@pytest.mark.unit
def test_wrong_passwords_lock_the_client_out_for_a_while(web):
    app, client = web
    now = [1000.0]
    throttle = app.state.login_throttle
    throttle.clock = lambda: now[0]
    for _ in range(throttle.max_failures - 1):
        assert client.get("/", auth=("x", "guess")).status_code == 401
    assert client.get("/", auth=None).status_code == 401          # no password yet: not a failure
    assert client.get("/").status_code == 200                     # still allowed, and it resets the count
    for _ in range(throttle.max_failures):
        client.get("/", auth=("x", "guess"))
    locked = client.get("/")                                      # even the right password waits
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    assert "Sai mật khẩu quá nhiều lần" in locked.text
    assert client.post("/analyze", data={"ticker": "FPT"}).status_code == 429
    assert client.get("/health").status_code == 200
    now[0] += throttle.lockout + 1
    assert client.get("/").status_code == 200


@pytest.mark.unit
def test_old_failures_fall_out_of_the_window(web):
    app, client = web
    now = [0.0]
    throttle = app.state.login_throttle
    throttle.clock = lambda: now[0]
    for _ in range(throttle.max_failures * 3):
        client.get("/", auth=("x", "guess"))
        now[0] += throttle.window / (throttle.max_failures - 1) + 1
    assert client.get("/").status_code == 200


@pytest.mark.unit
def test_the_throttle_table_stays_bounded():
    from vnbot.web import LoginThrottle

    throttle = LoginThrottle(max_failures=3, max_clients=50)
    for i in range(500):
        throttle.failure(f"10.0.{i // 250}.{i % 250}")
    assert len(throttle._failures) + len(throttle._locked) <= 50


@pytest.mark.unit
def test_the_quick_page_does_not_show_exception_details(web, monkeypatch):
    from tradingagents.quant import engine

    _, client = web

    def broken(*a, **k):
        raise RuntimeError("/srv/secret/path token=abc")

    monkeypatch.setattr(engine, "analyze", broken)
    page = client.post("/quick", data={"ticker": "FPT", "date": "2026-10-13"}).text
    assert "Không tính được" in page and "secret" not in page


@pytest.mark.unit
def test_a_server_error_page_hides_the_exception(settings, queue):  # noqa: F811
    object.__setattr__(settings, "web_password", "s3cret")

    def broken():
        raise RuntimeError("/srv/secret/ledger.json")

    trader = SimpleNamespace(momentum_book=broken, settings=settings, running=False)
    client = TestClient(create_app(settings, queue, Watchlist(settings), trader=trader),
                        raise_server_exceptions=False)
    response = client.get("/bot", auth=("u", "s3cret"))
    assert response.status_code == 500 and "secret" not in response.text and "Lỗi máy chủ" in response.text


@pytest.mark.unit
def test_unknown_pages_and_bad_forms_get_the_vietnamese_error_page(web):
    _, client = web
    missing = client.get("/khong-co")
    assert missing.status_code == 404 and "Không tìm thấy trang" in missing.text
    assert missing.headers["X-Frame-Options"] == "DENY"
    bad = client.post("/analyze", data={})
    assert bad.status_code == 422 and "Thiếu hoặc sai dữ liệu" in bad.text and "text/html" in bad.headers["content-type"]


@pytest.mark.unit
def test_the_web_watchlist_is_capped(web, settings):  # noqa: F811
    _, client = web
    response = client.post("/watchlist", data={"codes": ",".join(["FPT"] * 31)})
    assert "Tối đa 30" in response.text
    assert Watchlist(settings).get() == ["FPT", "HPG"]
