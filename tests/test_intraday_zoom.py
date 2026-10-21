"""Intraday alerts (no AI) and the Zoom channel ported from the OKX bot. No network."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tradingagents.trading.ledger import Ledger, Order, Position
from vnbot import settings as settings_module
from vnbot.intraday import IntradayMonitor
from vnbot.service import Watchlist
from vnbot.telegram_bot import TelegramBot
from vnbot.web import create_app
from vnbot.zoom import ZoomChannel, ZoomConfig, parse_event, split, to_zoom_markdown

DAY = "2026-10-21"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("TRADE_MODE", "INTRADAY_ALERTS", "INTRADAY_INTERVAL_MIN", "INTRADAY_VNI_DROP_PCT",
                 "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_IDS", "WEB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_MODE", "signal")
    return settings_module.load()


def _books():
    real = Ledger(cash=0.0, start_cash=0.0)
    real.positions["FPT"] = Position("FPT", 1000, 100_000.0, "2026-10-20", stop=95_000.0, target1=110_000.0,
                                     last_price=100_000.0, unsettled=[{"date": "2026-10-20", "qty": 400, "sessions": 0}])
    paper = Ledger(cash=1e9, start_cash=1e9)
    paper.positions["HPG"] = Position("HPG", 500, 30_000.0, "2026-10-01", stop=28_000.0, target1=33_000.0,
                                      target2=36_000.0, last_price=30_000.0)
    paper.orders = [Order("MWG", "BUY", 100, 60_000.0, "2026-10-20", stop=57_000.0),
                    Order("VCB", "BUY", 100, 90_000.0, "2026-10-20", stop=86_000.0),
                    Order("SSI", "BUY", 100, None, "2026-10-20")]
    return real, paper


def _monitor(settings, prices, change=None):
    real, paper = _books()
    trader = SimpleNamespace(real=lambda: real, paper=lambda: paper)
    notes = []
    mon = IntradayMonitor(settings, trader, notes.append, price=lambda code, day: prices.get(code),
                          index_change=lambda day: change, exchange_of=lambda code: "HOSE")
    return mon, notes


@pytest.mark.unit
def test_alerts_cover_stops_targets_zones_and_vnindex_once_a_day(settings):
    prices = {"FPT": 94_000.0, "HPG": 33_500.0, "MWG": 59_500.0, "VCB": 85_000.0, "SSI": 1.0}
    mon, notes = _monitor(settings, prices, change=-4.5)
    texts = mon.check(DAY)
    joined = "\n".join(texts)
    assert "⛔ 💼 Thật FPT: giá 94.000 ≤ stop 95.000, bán được 600/1.000 cp (T+2)" in joined
    assert "🎯 🤖 Ảo (Quant Engine) HPG: giá 33.500 chạm T1 33.000" in joined and "T2" not in joined
    assert "MWG giá ≤ 60.000: giá 59.500 đang trong vùng mua" in joined
    assert "VCB giá ≤ 90.000: giá 85.000 đã dưới stop dự kiến 86.000, nên bỏ lệnh" in joined
    assert "SSI" not in joined                                   # an order at the open has no zone
    assert joined.count("VNINDEX") == 2                         # -2% and -4% both crossed
    assert len(notes) == 1 and "không gọi AI" in notes[0]
    assert mon.check(DAY) == [] and len(notes) == 1              # nothing twice the same day
    mon2, _ = _monitor(settings, prices, change=-4.5)            # a restart remembers too
    assert mon2.check(DAY) == []
    assert len(mon2.check("2026-10-22")) == len(texts)           # a new day alerts again


@pytest.mark.unit
def test_a_floor_lock_and_the_paper_mode_book(settings):
    import dataclasses

    paper_mode = dataclasses.replace(settings, trade_mode="paper")
    mon, _ = _monitor(paper_mode, {"FPT": 50_000.0, "HPG": 27_900.0})
    joined = "\n".join(mon.check(DAY))
    assert "Thật" not in joined                                 # no real book outside signal mode
    assert "HPG: giá 27.900 = giá sàn 27.900" in joined and "≤ stop 28.000, bán được 0/500 cp" in joined


@pytest.mark.unit
def test_the_monitor_only_runs_in_continuous_trading(settings, monkeypatch):
    from datetime import datetime

    from tradingagents.market import vn_rules

    mon, _ = _monitor(settings, {})
    days = []
    mon.check = lambda day: days.append(day) or []
    at = {"now": datetime(2026, 10, 21, 9, 5, tzinfo=vn_rules.VN_TZ)}
    monkeypatch.setattr(vn_rules, "now_vn", lambda: at["now"])
    mon.tick()
    at["now"] = datetime(2026, 10, 21, 12, 0, tzinfo=vn_rules.VN_TZ)    # lunch
    mon.tick()
    assert days == []
    at["now"] = datetime(2026, 10, 21, 10, 0, tzinfo=vn_rules.VN_TZ)
    mon.tick()
    mon.tick()                                                         # within the interval
    assert days == [DAY]


@pytest.mark.unit
def test_intraday_settings(settings, monkeypatch):
    assert settings.intraday_alerts and settings.intraday_interval_min == 15 and settings.intraday_vni_drop_pct == 2.0
    monkeypatch.setenv("INTRADAY_ALERTS", "0")
    monkeypatch.setenv("INTRADAY_INTERVAL_MIN", "1")
    loaded = settings_module.load()
    assert not loaded.intraday_alerts and loaded.intraday_interval_min == 5


# ─── Zoom ────────────────────────────────────────────────────────────────────


class _Response:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.text = status, data or {}, ""

    def json(self):
        return self._data


def _zoom(**config):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if "oauth" in url:
            return _Response(200, {"access_token": "tok", "expires_in": 3600})
        return _Response(200)

    base = {"client_id": "id", "client_secret": "cs", "bot_jid": "bot@xmpp", "account_id": "acc",
            "secret_token": "sekret", "allowed": ("me@xmpp",)}
    zoom = ZoomChannel(ZoomConfig(**{**base, **config}), post=post, start=False)
    return zoom, calls


@pytest.mark.unit
def test_notifications_go_to_the_webhook_and_the_paired_jid():
    zoom, calls = _zoom(webhook_url="https://hook/x", webhook_token="wt", to_jid="me@xmpp")
    assert zoom.notify_on and zoom.commands_on
    zoom.send("**Báo cáo**\n| a | b |\n|---|---|")
    zoom.deliver(zoom.queue.get_nowait())
    urls = [u for u, _ in calls]
    assert urls[0] == "https://hook/x?format=message" and calls[0][1]["headers"]["Authorization"] == "wt"
    message = [kw for u, kw in calls if u.endswith("/im/chat/messages")][0]["json"]
    assert message["to_jid"] == "me@xmpp" and message["robot_jid"] == "bot@xmpp"
    assert message["content"]["body"][0]["text"] == "*Báo cáo*\n• a — b"


@pytest.mark.unit
def test_sharing_the_okx_app_is_notify_only_without_a_secret_token():
    zoom, _ = _zoom(secret_token="", to_jid="me@xmpp")
    assert zoom.notify_on and not zoom.commands_on


@pytest.mark.unit
def test_split_and_events():
    assert all(len(p) <= 100 for p in split("x " * 300, 100))
    assert to_zoom_markdown("## Tiêu đề") == "*Tiêu đề*"
    ev = parse_event({"payload": {"cmd": "nhanh FPT", "userJid": "me@xmpp", "toJid": "bot@xmpp",
                                  "accountId": "acc", "messageId": "m1"}})
    assert ev["reply_jid"] == "me@xmpp"                           # 1:1: back to the sender, not the bot
    channel = parse_event({"payload": {"cmd": "x", "userJid": "me@xmpp", "toJid": "ch@conf", "channelName": "c"}})
    assert channel["reply_jid"] == "ch@conf"


def _signed(zoom_secret, body):
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(zoom_secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"x-zm-signature": sig, "x-zm-request-timestamp": ts, "content-type": "application/json"}


@pytest.fixture
def telegram(settings):
    object.__setattr__(settings, "telegram_token", "t")
    object.__setattr__(settings, "telegram_allowed_ids", frozenset({111}))
    object.__setattr__(settings, "telegram_report_chat_ids", frozenset({111}))
    queue = SimpleNamespace(submit=lambda *a, **k: None, add_listener=lambda listener: None)
    bot = TelegramBot(settings, queue, Watchlist(settings))
    bot.sent = []
    bot._call = lambda method, http_timeout=30, **params: bot.sent.append((method, params)) or {}
    return bot


@pytest.mark.unit
def test_zoom_commands_run_like_telegram_ones_and_strangers_are_refused(settings, telegram, monkeypatch):
    from tradingagents.quant import engine

    monkeypatch.setattr(engine, "analyze", lambda code, day=None, **k: SimpleNamespace(
        action="NO_TRADE", deterministic_summary=f"{code} - NO_TRADE"))
    zoom, _ = _zoom()
    telegram.zoom = zoom
    zoom.handler = telegram.zoom_command
    zoom.alert_owner = telegram.broadcast
    client = TestClient(create_app(settings, SimpleNamespace(recent=lambda n=30: []), Watchlist(settings), zoom=zoom))

    body = json.dumps({"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}}).encode()
    r = client.post("/webhook/zoom", content=body, headers=_signed("sekret", body))
    assert r.json()["plainToken"] == "abc" and len(r.json()["encryptedToken"]) == 64
    assert client.post("/webhook/zoom", content=body, headers=_signed("wrong", body)).status_code == 401

    body = json.dumps({"event": "bot_notification", "payload": {
        "cmd": "nhanh hpg", "userJid": "me@xmpp", "toJid": "bot@xmpp", "accountId": "acc", "messageId": "m1"}}).encode()
    assert client.post("/webhook/zoom", content=body, headers=_signed("sekret", body)).json() == {"ok": True}
    for lane in telegram._lanes:
        lane.submit(lambda: None).result()                       # wait for the command
    kind, text, where = zoom.queue.get_nowait()
    assert (kind, text, where["to_jid"]) == ("reply", "HPG - NO_TRADE", "me@xmpp")

    body = json.dumps({"event": "bot_notification", "payload": {
        "cmd": "von 1", "userJid": "evil@xmpp", "toJid": "bot@xmpp", "messageId": "m2"}}).encode()
    client.post("/webhook/zoom", content=body, headers=_signed("sekret", body))
    replies = _drain(zoom, "reply")
    assert len(replies) == 1 and "chưa được ghép đôi" in replies[0][1] and replies[0][2]["to_jid"] == "evil@xmpp"
    assert any("/zoompair evil@xmpp" in p.get("text", "") for _, p in telegram.sent)   # the owner is told on Telegram
    assert zoom.pending()["jid"] == "evil@xmpp" and zoom.pairing()["jid"] == "me@xmpp"


def _drain(zoom, kind=None):
    items = []
    while not zoom.queue.empty():
        items.append(zoom.queue.get_nowait())
    return [i for i in items if kind is None or i[0] == kind]


@pytest.mark.unit
def test_pairing_like_the_okx_bot(tmp_path):
    zoom, _ = _zoom(allowed=())
    zoom.state = __import__("vnbot.zoom", fromlist=["ZoomState"]).ZoomState(tmp_path / "zoom_state.json")
    owner = []
    zoom.alert_owner = owner.append
    ran = []
    zoom.handler = lambda target, text: ran.append(text)
    msg = {"payload": {"cmd": "trangthai", "userJid": "me@xmpp", "toJid": "bot@xmpp", "accountId": "acc2",
                       "userName": "Tôi", "messageId": "a"}}
    zoom.handle(msg)
    assert ran == [] and "/zoompair me@xmpp" in owner[0] and not zoom.notify_on   # refused, owner told
    p = zoom.pair_pending()                                                       # the button on /zoom
    assert p["jid"] == "me@xmpp" and p["account_id"] == "acc2" and zoom.notify_on
    assert "Đã ghép đôi" in _drain(zoom, "reply")[-1][1]
    zoom.handle({**msg, "payload": {**msg["payload"], "messageId": "b", "cmd": "nhanh fpt"}})
    zoom.handle({**msg, "payload": {**msg["payload"], "messageId": "b", "cmd": "nhanh fpt"}})   # resent by Zoom
    assert ran == ["/nhanh fpt"]
    zoom.send("báo cáo")
    _, _, where = zoom.queue.get_nowait()
    assert zoom.notify_target() == {"to_jid": "me@xmpp", "user_jid": "me@xmpp", "account_id": "acc2"}
    restarted = ZoomChannel(zoom.config, state_path=tmp_path / "zoom_state.json", start=False)
    assert restarted.pairing()["jid"] == "me@xmpp"                                # kept across restarts
    zoom.lifecycle("app_deauthorized", {"account_id": "acc2"})
    assert zoom.pairing() == {} and "gỡ khỏi Zoom" in owner[-1]


@pytest.mark.unit
def test_signature_with_the_wrong_variable_is_named_and_requests_are_logged():
    zoom, _ = _zoom(secret_token="wrong", verification_token="")
    body = b'{"event":"bot_notification"}'
    ok, why = zoom.verify_detail(body, _signed("cs", body))                     # signed with the client secret
    assert ok and "ZOOM_SECRET_TOKEN đang sai" in why
    ok, why = zoom.verify_detail(body, _signed("nope", body))
    assert not ok and "không khớp" in why
    old = {**_signed("wrong", body), "x-zm-request-timestamp": str(int(time.time()) - 900)}
    assert zoom.verify_detail(body, old) == (False, f"timestamp lệch giờ hoặc không hợp lệ ({old['x-zm-request-timestamp']})")
    for i in range(25):
        zoom.record("bot_notification", True, "ok", {"payload": {"cmd": f"c{i}", "userName": "Tôi"}})
    log = zoom.requests_log()
    assert len(log) == 20 and log[0]["cmd"] == "c24"


@pytest.mark.unit
def test_telegram_pairing_commands(settings, telegram):
    zoom, _ = _zoom(allowed=())
    telegram.zoom = zoom
    for text in ("/zoomstatus", "/zoompair me@xmpp Tôi", "/zoomstatus", "/zoomxoa", "/zoomstatus"):
        telegram._handle({"chat": {"id": 111}, "from": {"id": 111}, "text": text})
    texts = [p.get("text", "") for m, p in telegram.sent if m == "sendMessage"]
    assert texts[0] == "Chưa ghép đôi JID Zoom nào." and "Đã ghép đôi Zoom: me@xmpp" in texts[1]
    assert texts[2] == "Zoom đang ghép đôi: Tôi me@xmpp" and texts[3] == "Đã gỡ ghép đôi Zoom."
    assert texts[4] == "Chưa ghép đôi JID Zoom nào."
    telegram._handle({"chat": {"id": "zoom:1"}, "from": {"id": "zoom"}, "text": "/zoomxoa"})   # not from Zoom itself


@pytest.mark.unit
def test_public_pages_and_the_zoom_setup_page(settings, monkeypatch):
    from vnbot import pages

    object.__setattr__(settings, "web_password", "pw")
    object.__setattr__(settings, "public_url", "https://vn.bot-keep.xyz")
    object.__setattr__(settings, "support_email", "me@example.com")
    zoom, _ = _zoom()
    zoom.state = __import__("vnbot.zoom", fromlist=["ZoomState"]).ZoomState(settings.data_dir / "zoom_state.json")
    client = TestClient(create_app(settings, SimpleNamespace(recent=lambda n=30: [], is_active=lambda: False),
                                   Watchlist(settings), zoom=zoom))
    for path, text in (("/privacy", "Privacy Policy"), ("/terms", "Terms of Use"), ("/support", "me@example.com"),
                       ("/docs", "/vnbot nhanh FPT"), ("/data-deletion", "Data Deletion")):
        r = client.get(path)
        assert r.status_code == 200 and text in r.text, path               # public: no password
    assert client.get("/zoom").status_code == 401                          # the setup page is not
    r = client.get("/zoom/install", follow_redirects=False)
    assert r.status_code == 302 and "redirect_uri=https%3A%2F%2Fvn.bot-keep.xyz%2Fzoom%2Foauth%2Fcallback" in r.headers["location"]
    state = next(iter(zoom.state.get("oauth_states")))

    monkeypatch.setattr(pages.requests, "post", lambda *a, **k: _Response(200, {"access_token": "t", "scope": "imchat:bot"}))
    r = client.get(f"/zoom/oauth/callback?code=abcdefghijklmnop&state={state}")
    assert "Đã cài" in r.text and zoom.state.get("install")["scope"] == "imchat:bot"
    monkeypatch.setattr(pages.requests, "post", lambda *a, **k: _Response(400, {"error": "invalid_grant"}))
    assert "Đã cài" in client.get("/zoom/oauth/callback?code=abcdefghijklmnop").text   # the same code twice

    zoom.state.set("pending", {"jid": "me@xmpp", "name": "Tôi", "account_id": "acc", "reply_jid": "me@xmpp", "ts": time.time()})
    zoom.record("bot_notification", False, "chữ ký không khớp", {"payload": {"cmd": "nhanh"}})
    page = client.get("/zoom", auth=("u", "pw")).text
    assert "https://vn.bot-keep.xyz/webhook/zoom" in page and "https://vn.bot-keep.xyz/privacy" in page
    assert "Ghép đôi tài khoản này" in page and "chữ ký không khớp" in page and "SUPPORT_EMAIL" in page
    r = client.post("/zoom/pair", auth=("u", "pw"), headers={"Origin": "https://vn.bot-keep.xyz"}, follow_redirects=False)
    assert r.status_code == 303 and zoom.pairing()["jid"] == "me@xmpp"
    assert client.post("/zoom/pair", auth=("u", "pw"), headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.unit
def test_app_deauthorized_through_the_webhook(settings):
    zoom, _ = _zoom()
    zoom.pair("me@xmpp")
    client = TestClient(create_app(settings, SimpleNamespace(recent=lambda n=30: []), Watchlist(settings), zoom=zoom))
    body = json.dumps({"event": "app_deauthorized", "payload": {"account_id": "acc"}}).encode()
    assert client.post("/zoom/bot", content=body, headers=_signed("sekret", body)).json() == {"ok": True}
    assert zoom.pairing() == {} and zoom.requests_log()[0]["event"] == "app_deauthorized"
