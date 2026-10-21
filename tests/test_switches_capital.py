"""Channel switches on /ai, the paper capital form on /bot, the logo. No network."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vnbot import settings as settings_module
from vnbot.channels import Switches
from vnbot.service import Watchlist
from vnbot.telegram_bot import TelegramBot
from vnbot.trader import TradingBot
from vnbot.web import create_app

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("TRADE_MODE", "INTRADAY_ALERTS", "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_IDS", "WEB_PASSWORD", "PAPER_CAPITAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    loaded = settings_module.load()
    object.__setattr__(loaded, "web_password", "pw")
    return loaded


def _client(settings, trader=None, zoom=None, switches=None):
    queue = SimpleNamespace(recent=lambda n=30: [], is_active=lambda: False)
    client = TestClient(create_app(settings, queue, Watchlist(settings), trader=trader, zoom=zoom, switches=switches))
    client.auth = ("u", "pw")
    return client


@pytest.mark.unit
def test_switches_persist_and_default(tmp_path):
    sw = Switches(tmp_path, {"intraday": False})
    assert sw.on("telegram") and not sw.on("intraday")
    sw.set("telegram", False)
    sw.set("intraday", True)
    again = Switches(tmp_path, {"intraday": False})
    assert not again.on("telegram") and again.on("intraday")
    with pytest.raises(ValueError):
        sw.set("email", True)


@pytest.mark.unit
def test_broadcast_follows_the_switches(settings):
    object.__setattr__(settings, "telegram_token", "t")
    object.__setattr__(settings, "telegram_report_chat_ids", frozenset({111}))
    bot = TelegramBot(settings, SimpleNamespace(add_listener=lambda f: None), Watchlist(settings))
    sent, zoomed = [], []
    bot._call = lambda method, http_timeout=30, **params: sent.append(params) or {}
    bot.zoom = SimpleNamespace(send=zoomed.append)
    bot.switches = Switches(settings.data_dir)
    bot.broadcast("a")
    bot.switches.set("telegram", False)
    bot.broadcast("b")
    bot.switches.set("zoom_notify", False)
    bot.broadcast("c")
    assert [p["text"] for p in sent] == ["a"] and zoomed == ["a", "b"]


@pytest.mark.unit
def test_intraday_alerts_follow_their_switch(settings, monkeypatch):
    from datetime import datetime

    from tradingagents.market import vn_rules
    from vnbot.intraday import IntradayMonitor

    sw = Switches(settings.data_dir)
    mon = IntradayMonitor(settings, SimpleNamespace(), lambda t: None, switches=sw)
    days = []
    mon.check = lambda day: days.append(day) or []
    monkeypatch.setattr(vn_rules, "now_vn", lambda: datetime(2026, 10, 21, 10, 0, tzinfo=vn_rules.VN_TZ))
    sw.set("intraday", False)
    mon.tick()
    assert days == []
    sw.set("intraday", True)
    mon.tick()
    assert days == ["2026-10-21"]


@pytest.mark.unit
def test_the_ai_page_switches_channels_for_real(settings):
    sw = Switches(settings.data_dir)
    zoom = SimpleNamespace(notify_on=True, commands_on=False)
    client = _client(settings, zoom=zoom, switches=sw)
    page = client.get("/ai").text
    assert 'action="/channels/zoom_notify"' in page and 'aria-checked="true"' in page
    assert "chưa cấu hình" in page                          # Telegram without a token, Zoom commands off
    assert 'action="/channels/telegram"' not in page
    r = client.post("/channels/zoom_notify", data={"on": "0"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and not sw.on("zoom_notify")
    assert 'aria-checked="false"' in client.get("/ai").text
    assert client.post("/channels/zoom_notify", data={"on": "1"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/channels/sms", data={"on": "1"}, headers=ORIGIN).status_code == 404


@pytest.mark.unit
def test_zoom_commands_switched_off_are_ignored(settings):
    import hashlib
    import hmac
    import time

    from vnbot.zoom import ZoomChannel, ZoomConfig

    zoom = ZoomChannel(ZoomConfig(client_id="i", client_secret="c", bot_jid="b", secret_token="s", allowed=("me",)),
                       start=False)
    ran = []
    zoom.handler = lambda target, text: ran.append(text)
    sw = Switches(settings.data_dir)
    sw.set("zoom_commands", False)
    client = _client(settings, zoom=zoom, switches=sw)
    body = json.dumps({"event": "bot_notification", "payload": {"cmd": "nhanh", "userJid": "me", "messageId": "x"}}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"s", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    r = client.post("/webhook/zoom", content=body, headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts})
    assert r.json()["ignored"] and ran == [] and "đang tắt" in zoom.requests_log()[0]["reason"]


@pytest.mark.unit
def test_paper_capital_restarts_the_chosen_books(settings):
    trader = TradingBot(settings, Watchlist(settings))
    old = trader.paper()
    old.cash = 123.0
    old.save(trader.paper_path)
    assert trader.paper_capital() == settings.paper_capital
    with pytest.raises(ValueError, match="Chọn ít nhất"):
        trader.reset_paper(2e9, [])
    with pytest.raises(ValueError, match="Vốn phải"):
        trader.reset_paper(1e6, ["engine"])
    msg = trader.reset_paper(2_500_000_000, ["engine", "momentum"])
    assert "2.500.000.000 đồng" in msg and "Quant Engine, Động lượng" in msg
    assert trader.paper().cash == 2_500_000_000 and trader.momentum_book().cash == 2_500_000_000
    assert list(trader.dir.glob("paper.json.reset-*"))                    # the old book is kept
    trader._running.set()
    with pytest.raises(ValueError, match="đang chạy"):
        trader.reset_paper(3e9, ["factor"])


@pytest.mark.unit
def test_the_bot_page_form_sets_the_capital(settings):
    trader = TradingBot(settings, Watchlist(settings))
    client = _client(settings, trader=trader)
    page = client.get("/bot").text
    assert 'action="/bot/capital"' in page and "1.000.000.000" in page
    r = client.post("/bot/capital", data={"capital": "500.000.000", "books": ["factor"]}, headers=ORIGIN,
                    follow_redirects=False)
    assert "x%C3%A1c%20nh%E1%BA%ADn" in r.headers["location"]          # unconfirmed: "xác nhận" asked
    assert trader.factor_book().cash == settings.paper_capital
    r = client.post("/bot/capital", data={"capital": "500.000.000", "books": ["factor"], "confirm": "1"},
                    headers=ORIGIN, follow_redirects=True)
    assert "500.000.000 đồng" in r.text and trader.factor_book().cash == 500_000_000
    assert not trader.paper_path.exists()        # the engine book was not chosen: untouched (never saved here)


@pytest.mark.unit
def test_the_logo_is_served_and_shown(settings):
    from vnbot.boot import LateApp

    client = _client(settings)
    r = client.get("/static/logo.png", auth=None)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert client.get("/static/../settings.py").status_code == 404
    assert '<img class="logo" src="/static/logo.png"' in client.get("/").text
    assert '<img class="logo" src="/static/logo.png"' in client.get("/privacy", auth=None).text
    assert TestClient(LateApp()).get("/static/logo.png").content[:4] == b"\x89PNG"   # during the start too


@pytest.mark.unit
def test_the_zoom_page_no_longer_offers_the_okx_app(settings):
    page = _client(settings).get("/zoom").text
    assert "Chỉ muốn nhận thông báo bằng app chatbot của bot OKX" not in page
