"""The daily watchlist run screens with the engine first and gives the AI only a few codes.

Before, every watchlist code (up to 30) got the full multi-agent analysis each
weekday, some 25 AI requests each, which knocked over the owner's 9Router and
OpenRouter. Like the OKX bot's scan, the engine (no AI) now looks at every
code and only the strongest BUY/SELL ones go to the AI, within the day's budget.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from tradingagents.llm_clients import fallback_client as fc
from vnbot import service, settings as settings_module
from vnbot.service import DailyScheduler

ACTIONS = {"FPT": ("BUY", 0.80), "HPG": ("BUY", 0.90), "VCB": ("NO_TRADE", 0.30),
           "MWG": ("SELL", 0.70), "SSI": ("WATCH", 0.60)}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("DAILY_AI_MAX", "DAILY_AI_ACTIONS", "WATCHLIST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return settings_module.load()


def _run(settings, monkeypatch, codes, fail=()):
    submitted, notes = [], []
    queue = SimpleNamespace(submit=lambda code, day, source: submitted.append(code))
    watchlist = SimpleNamespace(get=lambda: list(codes))
    sched = DailyScheduler(settings, queue, watchlist, notify=notes.append)
    monkeypatch.setattr(DailyScheduler, "_is_trading_day", staticmethod(lambda day: True))

    def quick(code, day):
        if code in fail:
            raise RuntimeError("vendor down")
        action, confidence = ACTIONS[code]
        return SimpleNamespace(action=action, confidence=confidence)

    monkeypatch.setattr(sched, "_quick", quick)
    sched._report("2026-10-14")
    return sched, submitted, notes


@pytest.mark.unit
def test_only_the_strongest_buy_sell_codes_get_the_ai(settings, monkeypatch):
    settings = dataclasses.replace(settings, daily_ai_max=2)
    sched, submitted, notes = _run(settings, monkeypatch, ["FPT", "HPG", "VCB", "MWG", "SSI"])
    assert submitted == ["HPG", "FPT"]                  # by engine confidence; MWG waits
    assert sched._stamp.read_text() == "2026-10-14"
    text = notes[0]
    assert "HPG, FPT" in text and "MWG" in text and "VCB: NO_TRADE (30%)" in text and "SSI: WATCH" in text


@pytest.mark.unit
def test_no_ai_when_nothing_calls_for_a_decision(settings, monkeypatch):
    _, submitted, notes = _run(settings, monkeypatch, ["VCB", "SSI"], fail=("SSI",))
    assert submitted == []
    assert "VCB: NO_TRADE" in notes[0] and "Lỗi dữ liệu: SSI" in notes[0]


@pytest.mark.unit
def test_the_day_budget_limits_the_daily_analyses(settings, monkeypatch):
    monkeypatch.setattr(fc, "budget_left", lambda: service.CALLS_PER_ANALYSIS + 3)
    _, submitted, notes = _run(settings, monkeypatch, ["FPT", "HPG", "MWG"])
    assert submitted == ["HPG"]
    assert "hạn mức AI" in notes[0]


@pytest.mark.unit
def test_settings_read_the_daily_ai_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAILY_AI_MAX", "0")
    monkeypatch.setenv("DAILY_AI_ACTIONS", "buy, sell, watch")
    loaded = settings_module.load()
    assert loaded.daily_ai_max == 0 and loaded.daily_ai_actions == ("BUY", "SELL", "WATCH")
