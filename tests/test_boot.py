"""The web port answers from the first second (vnbot.boot), like the OKX bot."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vnbot.boot import LateApp


@pytest.mark.unit
def test_the_port_answers_while_starting_then_hands_over():
    web = LateApp()
    with TestClient(web) as client:                 # runs the lifespan too
        health = client.get("/health")
        assert health.status_code == 200 and health.json() == {"ok": True, "booting": True}
        page = client.get("/")
        assert page.status_code == 503 and page.headers["retry-after"] == "5" and "Đang khởi động" in page.text
        assert client.post("/webhook/zoom", content=b"{}").status_code == 503     # Zoom retries later

        app = FastAPI()
        app.get("/")(lambda: {"real": True})
        web.ready(app)
        assert client.get("/").json() == {"real": True}


@pytest.mark.unit
def test_a_failed_start_is_shown_instead_of_a_silent_port():
    web = LateApp()
    web.failed("ModuleNotFoundError: No module named 'x'")
    client = TestClient(web)
    health = client.get("/health")
    assert health.status_code == 500 and "ModuleNotFoundError" in health.json()["error"]
    page = client.get("/bot")
    assert page.status_code == 500 and "Khởi động lỗi" in page.text and "log" in page.text


@pytest.mark.unit
def test_app_port_is_read_after_server_port_and_port(monkeypatch, tmp_path):
    from vnbot import settings as settings_module

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in ("SERVER_PORT", "PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_PORT", "3001")
    assert settings_module.load().port == 3001
    monkeypatch.setenv("SERVER_PORT", "4000")
    assert settings_module.load().port == 4000
