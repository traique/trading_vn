"""The daily watchlist run screens with the engine first and asks the AI the OKX way.

Before, every watchlist code (up to 30) got the full multi-agent analysis each
weekday, some 25 AI requests each, which knocked over the owner's 9Router and
OpenRouter. Like the OKX bot's scan, the engine (no AI) now looks at every
code and only the strongest BUY/SELL ones go to the AI, within the day's
budget: by default one critic request each, whose verdicts are kept and
settled on the bars that followed (were its objections right?).
"""

import dataclasses
from types import SimpleNamespace

import pytest

from tradingagents.llm_clients import fallback_client as fc
from vnbot import critic, service, settings as settings_module
from vnbot.service import DailyScheduler

ACTIONS = {"FPT": ("BUY", 0.80), "HPG": ("BUY", 0.90), "VCB": ("NO_TRADE", 0.30),
           "MWG": ("SELL", 0.70), "SSI": ("WATCH", 0.60)}


def _result(code):
    action, confidence = ACTIONS[code]
    return SimpleNamespace(symbol=code, action=action, confidence=confidence, price=100.0,
                           stop_price=95.0 if action == "BUY" else None,
                           target_price=110.0 if action == "BUY" else None,
                           deterministic_summary=f"{code} - tín hiệu {action}", reasons=[], scenarios=[])


class FakeLLM:
    """Answers each request with the next scripted reply; records the prompts."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, messages):
        self.prompts.append(messages[-1].content)
        reply = self.replies.pop(0) if self.replies else '{"verdict": "AGREE", "confidence": 60, "reason": "ok"}'
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("DAILY_AI_MAX", "DAILY_AI_ACTIONS", "DAILY_AI_MODE", "WATCHLIST", "SCAN_UNIVERSE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCAN_UNIVERSE", "watchlist")       # the index screen has its own tests
    monkeypatch.setattr(critic, "bars_after", lambda code, day: [])     # no shadow results yet
    return settings_module.load()


def _run(settings, monkeypatch, codes, fail=(), llm=None):
    submitted, notes = [], []
    queue = SimpleNamespace(submit=lambda code, day, source: submitted.append(code))
    watchlist = SimpleNamespace(get=lambda: list(codes))
    llm = llm or FakeLLM()
    sched = DailyScheduler(settings, queue, watchlist, notify=notes.append, critic_llm=llm)
    monkeypatch.setattr(DailyScheduler, "_is_trading_day", staticmethod(lambda day: True))

    def quick(code, day):
        if code in fail:
            raise RuntimeError("vendor down")
        return _result(code)

    monkeypatch.setattr(sched, "_quick", quick)
    sched._report("2026-10-14")
    return sched, submitted, notes, llm


@pytest.mark.unit
def test_the_critic_asks_once_per_strongest_code(settings, monkeypatch):
    settings = dataclasses.replace(settings, daily_ai_max=2)
    llm = FakeLLM('{"verdict": "REJECT", "confidence": 75, "reason": "VNINDEX yếu", "risks": ["khối ngoại bán"]}',
                  'Đây: {"verdict": "AGREE", "confidence": 65, "reason": "dòng tiền tốt"}')
    sched, submitted, notes, _ = _run(settings, monkeypatch, ["FPT", "HPG", "VCB", "MWG", "SSI"], llm=llm)
    assert submitted == []                              # no multi-agent job in critic mode
    assert len(llm.prompts) == 2 and "HPG" in llm.prompts[0] and "FPT" in llm.prompts[1]
    text = notes[0]
    assert "HPG BUY (90%) → AI PHẢN ĐỐI 75%: VNINDEX yếu" in text and "khối ngoại bán" in text
    assert "FPT BUY (80%) → AI ĐỒNG Ý 65%" in text
    assert "MWG" in text and "VCB: NO_TRADE (30%)" in text and "SSI: WATCH" in text
    rows = sched.critic_log.rows()
    assert [(r["code"], r["verdict"]) for r in rows] == [("HPG", "REJECT"), ("FPT", "AGREE")]
    assert sched._stamp.read_text() == "2026-10-14"


@pytest.mark.unit
def test_the_screen_covers_vn100_plus_the_watchlist(settings, monkeypatch):
    from tradingagents.dataflows import money_flows as mf

    asked = []

    def members(group):
        asked.append(group)
        return {"VN100": ["HPG", "MWG", "VCB"], "VN30": ["HPG"]}.get(group, [])

    monkeypatch.setattr(mf, "index_members", members)
    vn100 = dataclasses.replace(settings, scan_universe="vn100", daily_ai_max=1)
    sched, _, notes, llm = _run(vn100, monkeypatch, ["FPT", "SSI"])
    assert asked == ["VN100"] and len(llm.prompts) == 1 and "HPG" in llm.prompts[0]
    text = notes[0]
    assert "Quét VN100 + watchlist" in text and "đã xét 5 mã" in text
    assert "BUY 2" in text and "Đạt nhưng chưa gọi AI" in text and "FPT, MWG" in text
    assert "• SSI: WATCH" in text and "• VCB" not in text          # only the watchlist is listed one by one
    monkeypatch.setattr(mf, "index_members", lambda group: [] if group == "VN100" else ["HPG"])
    sched, _, notes, _ = _run(vn100, monkeypatch, ["FPT"])
    assert "đã xét 2 mã" in notes[0]                                  # VN100 down: VN30 instead


@pytest.mark.unit
def test_an_unreadable_or_failed_answer_is_reported_not_kept(settings, monkeypatch):
    llm = FakeLLM("tôi nghĩ là nên mua", RuntimeError("Mọi nhà cung cấp AI đang nghỉ"))
    sched, _, notes, _ = _run(settings, monkeypatch, ["HPG", "FPT"], llm=llm)
    assert "HPG BUY (90%) → AI không trả lời: AI trả lời không đúng định dạng" in notes[0]
    assert "FPT BUY (80%) → AI không trả lời: Mọi nhà cung cấp" in notes[0]
    assert sched.critic_log.rows() == []


@pytest.mark.unit
def test_full_mode_queues_the_agents(settings, monkeypatch):
    settings = dataclasses.replace(settings, daily_ai_mode="full", daily_ai_max=2)
    _, submitted, notes, llm = _run(settings, monkeypatch, ["FPT", "HPG", "VCB", "MWG"])
    assert submitted == ["HPG", "FPT"] and llm.prompts == []
    assert "AI phân tích đầy đủ: HPG, FPT" in notes[0]


@pytest.mark.unit
def test_off_mode_and_nothing_to_decide_make_no_request(settings, monkeypatch):
    _, submitted, notes, llm = _run(dataclasses.replace(settings, daily_ai_mode="off"), monkeypatch, ["FPT"])
    assert submitted == [] and llm.prompts == [] and "DAILY_AI_MODE=off" in notes[0]
    _, submitted, notes, llm = _run(settings, monkeypatch, ["VCB", "SSI"], fail=("SSI",))
    assert submitted == [] and llm.prompts == []
    assert "VCB: NO_TRADE" in notes[0] and "Lỗi dữ liệu: SSI" in notes[0]


@pytest.mark.unit
def test_the_day_budget_limits_the_requests(settings, monkeypatch):
    monkeypatch.setattr(fc, "budget_left", lambda: service.CALLS_PER_CRITIC * 2 + 1)
    _, _, notes, llm = _run(settings, monkeypatch, ["FPT", "HPG", "MWG"])
    assert len(llm.prompts) == 2 and "hạn mức AI" in notes[0]
    full = dataclasses.replace(settings, daily_ai_mode="full")
    monkeypatch.setattr(fc, "budget_left", lambda: service.CALLS_PER_ANALYSIS + 3)
    _, submitted, _, _ = _run(full, monkeypatch, ["FPT", "HPG", "MWG"])
    assert submitted == ["HPG"]


@pytest.mark.unit
def test_settings_read_the_daily_ai_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in ("DAILY_AI_MAX", "DAILY_AI_ACTIONS", "DAILY_AI_MODE"):
        monkeypatch.delenv(name, raising=False)
    loaded = settings_module.load()
    monkeypatch.delenv("SCAN_UNIVERSE", raising=False)
    loaded = settings_module.load()
    assert (loaded.daily_ai_mode, loaded.daily_ai_max, loaded.daily_ai_actions) == ("critic", 5, ("BUY", "SELL"))
    assert loaded.scan_universe == "vn100"
    monkeypatch.setenv("DAILY_AI_MODE", "full")
    monkeypatch.setenv("DAILY_AI_MAX", "0")
    monkeypatch.setenv("DAILY_AI_ACTIONS", "buy, sell, watch")
    loaded = settings_module.load()
    assert loaded.daily_ai_mode == "full" and loaded.daily_ai_max == 0
    assert loaded.daily_ai_actions == ("BUY", "SELL", "WATCH")


# ─── Shadow results: was the AI right? ──────────────────────────────────────


def _bar(high, low, close):
    return {"High": high, "Low": low, "Close": close}


@pytest.mark.unit
def test_a_buy_settles_on_stop_target_or_time():
    row = {"action": "BUY", "price": 100.0, "stop": 95.0, "target": 110.0}
    assert critic.settle_one(row, [_bar(104, 99, 103), _bar(111, 101, 109)])["r"] == 2.0
    assert critic.settle_one(row, [_bar(112, 94, 100)])["r"] == -1.0          # both in one session: stop first
    assert critic.settle_one(row, [_bar(104, 99, 103)] * 5) is None           # undecided yet
    out = critic.settle_one(row, [_bar(104, 99, 103)] * critic.HOLD_SESSIONS)
    assert out == {"r": 0.6, "label": "hết thời gian", "sessions": critic.HOLD_SESSIONS}
    sell = {"action": "SELL", "price": 100.0, "stop": 105.0, "target": None}
    assert critic.settle_one(sell, [_bar(99, 90, 92)] * critic.SELL_SESSIONS)["label"] == "giá giảm"


@pytest.mark.unit
def test_shadow_results_give_stats_and_lessons(tmp_path):
    log = critic.CriticLog(tmp_path)
    for day, code, verdict in [("2026-10-01", "FPT", "REJECT"), ("2026-10-02", "HPG", "REJECT"),
                               ("2026-10-03", "VNM", "AGREE")]:
        result = SimpleNamespace(symbol=code, action="BUY", price=100.0, stop_price=95.0, target_price=110.0)
        log.add(day, result, {"verdict": verdict, "confidence": 70, "reason": "x", "risks": []})
    paths = {"FPT": [_bar(101, 94, 95)], "HPG": [_bar(111, 99, 110)], "VNM": [_bar(111, 99, 110)]}
    assert log.settle(lambda code, day: paths[code]) == 3
    stats = log.stats()
    assert stats["REJECT"] == {"n": 2, "avg_r": 0.5, "win": 50} and stats["veto_right_pct"] == 50
    assert stats["AGREE"]["n"] == 1 and stats["pending"] == 0
    lessons = log.lessons()
    assert lessons[0] == "- 2026-10-01 FPT BUY: AI phản đối 70% → chạm stop, -1.00R (AI đúng)"
    assert lessons[1].endswith("(AI sai)")
    assert "AI phản đối đúng 50%" in log.stats_text()
    llm = FakeLLM()
    critic.ask(_result("FPT"), llm, lessons)
    assert "AI phản đối 70% → chạm stop" in llm.prompts[0]


@pytest.mark.unit
def test_parse_reads_only_a_valid_verdict():
    assert critic.parse('```json\n{"verdict": "agree", "confidence": "140", "reason": "ok"}\n```') == {
        "verdict": "AGREE", "confidence": 100, "reason": "ok", "risks": []}
    assert critic.parse('<think>dùng {x} rồi {"a": 1}</think> {"verdict": "REJECT", "confidence": 55}')["verdict"] == "REJECT"
    assert critic.parse('{"verdict": "MAYBE"}') is None
    assert critic.parse("không có json") is None
