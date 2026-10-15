"""Runtime fixes of the trading bot: when it runs, what it processes, and its state files.

No network: prices, the VN30 basket, the listing and the engine are stubbed;
the clock (Vietnam time) is fixed per test.
"""

import json
import math
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from vnbot import service, settings as settings_module, trader as trader_module
from vnbot.jobs import write_text_atomic
from vnbot.service import DailyScheduler, Watchlist

DAYS = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12",
        "2026-10-13", "2026-10-14"]


def _vn(y, mo, d, h, mi):
    from tradingagents.market import vn_rules

    return datetime(y, mo, d, h, mi, tzinfo=vn_rules.VN_TZ)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_IDS", "ALLOWED_USER_ID", "WEB_PASSWORD", "WATCHLIST",
                 "PUBLIC_URL", "TELEGRAM_REPORT_CHAT_IDS", "DAILY_REPORT_TIME", "TRADE_TIME", "TRADE_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WATCHLIST", "FPT")
    return settings_module.load()


@pytest.fixture
def clock(monkeypatch):
    from tradingagents.market import vn_rules

    at = {"now": _vn(2026, 10, 14, 16, 0)}
    monkeypatch.setattr(vn_rules, "now_vn", lambda: at["now"])
    return at


def _result(symbol, day, close, action):
    from tradingagents.quant.engine import QuantResult

    plan = action == "BUY"
    return QuantResult(
        symbol=symbol, as_of_date=day, last_bar_date=day, price=close, exchange="HOSE",
        action=action, confidence=0.8, setup_type="pullback", market_regime="risk_on", risk_level="low",
        data_quality="ok", reasons=[], stop_price=close * 0.9 if plan else None,
        target_price=close * 1.2 if plan else None, rr_ratio=2.0 if plan else None, invalidation_reason=None,
        entry_low=close - 500 if plan else None, entry_high=close + 200 if plan else None,
        target2=close * 1.3 if plan else None, position_size_pct=10.0 if plan else None,
    )


@pytest.fixture
def market(monkeypatch, clock):
    """Prices per (symbol, day) as (open, high, low, close, volume); loads and analyses are recorded."""
    from tradingagents.dataflows import money_flows, ohlcv
    from tradingagents.dataflows.vendors import vci
    from tradingagents.quant import engine

    rows = {}
    for i, day in enumerate(DAYS):
        rows[("VNINDEX", day)] = (1300 + i, 1305 + i, 1295 + i, 1300 + i, 1e6)
        c = 50_000 + 100 * i
        rows[("FPT", day)] = (c, c + 500, c - 1000, c, 1e6)
        rows[("HPG", day)] = (25_000, 25_200, 24_800, 25_000, 1e6)
    state = SimpleNamespace(rows=rows, loads=[], analyses=[], actions={"FPT": "BUY"}, gate=None)

    def load(symbol, as_of, fill_gaps=True, sources=None):
        state.loads.append((symbol, as_of, fill_gaps))
        out = [{"Date": pd.Timestamp(d), "Open": v[0], "High": v[1], "Low": v[2], "Close": v[3], "Volume": v[4]}
               for (s, d), v in sorted(state.rows.items()) if s == symbol and d <= as_of]
        frame = pd.DataFrame(out)
        if fill_gaps and len(frame):
            frame = frame.ffill()
        return frame

    def analyze(symbol, day, portfolio=None, params=None):
        if state.gate is not None:
            state.gate.wait(5)
        state.analyses.append((symbol, day))
        close = state.rows[(symbol, day)][3]
        return _result(symbol, day, close, state.actions.get(symbol, "NO_TRADE"))

    monkeypatch.setattr(ohlcv, "load_ohlcv", load)
    monkeypatch.setattr(money_flows, "index_members", lambda group="VN30": ["FPT", "HPG"])
    monkeypatch.setattr(engine, "analyze", analyze)
    monkeypatch.setattr(vci, "fetch_listing", lambda: [{"symbol": "FPT", "board": "HSX"},
                                                      {"symbol": "HPG", "board": "HSX"}])
    return state


@pytest.fixture
def bot(settings, market, monkeypatch):
    b = trader_module.TradingBot(settings, Watchlist(settings))
    monkeypatch.setattr(b, "universe", lambda: ["FPT", "HPG"])
    return b


# ─── 1. When the bot may run ─────────────────────────────────────────────────


@pytest.mark.unit
def test_the_trading_time_is_never_before_the_days_bar_is_final(settings, monkeypatch):
    monkeypatch.setenv("DAILY_REPORT_TIME", "09:05")
    s = settings_module.load()
    assert s.daily_report_time == "09:05" and s.trade_time == "15:30"     # report early, trade after close
    monkeypatch.setenv("DAILY_REPORT_TIME", "16:10")
    assert settings_module.load().trade_time == "16:10"
    monkeypatch.setenv("TRADE_TIME", "10:00")
    assert settings_module.load().trade_time == settings_module.MIN_TRADE_TIME == "15:05"
    monkeypatch.setenv("TRADE_TIME", "soon")
    assert settings_module.load().trade_time == "15:30"
    monkeypatch.setenv("TRADE_TIME", "17:45")
    assert settings_module.load().trade_time == "17:45"


@pytest.mark.unit
def test_a_session_is_final_only_from_1505_vietnam_time(clock):
    clock["now"] = _vn(2026, 10, 14, 15, 4)
    assert trader_module.final_day() == "2026-10-13"
    clock["now"] = _vn(2026, 10, 14, 15, 5)
    assert trader_module.final_day() == "2026-10-14"
    clock["now"] = _vn(2026, 10, 14, 8, 0)
    assert trader_module.final_day() == "2026-10-13"


@pytest.mark.unit
def test_a_run_during_the_session_processes_only_closed_sessions(bot, market, clock):
    # The provider serves today's forming bar; the bot must not take it as the close.
    clock["now"] = _vn(2026, 10, 14, 10, 30)
    report = bot.run_day("2026-10-14")
    assert "BOT PAPER 2026-10-13" in report
    paper = bot.paper()
    assert paper.meta["last_day"] == "2026-10-13" and paper.history[-1]["date"] == "2026-10-13"
    assert all(as_of <= "2026-10-13" for _, as_of, _ in market.loads)
    assert bot.session_day() == "2026-10-13"
    clock["now"] = _vn(2026, 10, 14, 15, 30)
    bot.run_day("2026-10-14")
    assert bot.paper().meta["last_day"] == "2026-10-14"


@pytest.mark.unit
def test_before_the_close_the_watchlist_run_does_not_mistake_today_for_a_holiday(settings, clock, monkeypatch):
    from tradingagents.dataflows import ohlcv

    def boom(*a, **k):
        raise AssertionError("no data exists yet for today")

    monkeypatch.setattr(ohlcv, "load_ohlcv", boom)
    clock["now"] = _vn(2026, 10, 14, 9, 5)
    assert DailyScheduler._is_trading_day("2026-10-14") is True


# ─── 2. Idempotency: each book has its own last processed day ───────────────


@pytest.mark.unit
def test_a_failure_mid_run_is_resumed_without_replaying_any_book(bot, market, monkeypatch):
    bot.run_day("2026-10-07")
    original = trader_module.TradingBot._momentum_session
    calls = {"n": 0}

    def flaky(self, day, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("VN30 down")
        return original(self, day, *args, **kwargs)

    monkeypatch.setattr(trader_module.TradingBot, "_momentum_session", flaky)
    with pytest.raises(ConnectionError):
        bot.run_day("2026-10-08")
    paper = bot.paper()
    assert paper.meta["last_day"] == "2026-10-08" and bot.last_report()["day"] == "2026-10-07"
    fills, history, positions = len(paper.fills), len(paper.history), {s: p.sessions for s, p in paper.positions.items()}
    analysed = len(market.analyses)

    report = bot.run_day("2026-10-08")                       # the retry
    paper = bot.paper()
    assert len(paper.fills) == fills and len(paper.history) == history
    assert {s: p.sessions for s, p in paper.positions.items()} == positions
    assert len(market.analyses) == analysed                  # the saved decisions were reused
    assert len(paper.orders) == len({(o.symbol, o.side) for o in paper.orders})   # no duplicate orders
    mom = bot.momentum_book()
    assert mom.meta["last_day"] == "2026-10-08" and [h["date"] for h in mom.history].count("2026-10-08") == 1
    assert "BOT PAPER 2026-10-08" in report and bot.last_report()["day"] == "2026-10-08"
    assert bot.run_day("2026-10-08") == report and len(market.analyses) == analysed


@pytest.mark.unit
def test_a_finished_session_is_not_reported_again(bot, market):
    first = bot.catch_up("2026-10-13")
    again = bot.catch_up("2026-10-13")
    assert first.new and not again.new and again.text == first.text
    assert bot.is_done("2026-10-13")


@pytest.mark.unit
def test_state_files_are_written_atomically(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_text_atomic(path, '{"day": "old"}')

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("vnbot.jobs.os.replace", broken_replace)
    with pytest.raises(OSError):
        write_text_atomic(path, '{"day": "new"}')
    assert json.loads(path.read_text()) == {"day": "old"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]        # no temporary left behind


# ─── 3. Missed sessions are caught up, in order, point in time ──────────────


@pytest.mark.unit
def test_missed_sessions_are_replayed_in_order_with_the_data_of_each_session(bot, market):
    bot.run_day("2026-10-07")
    assert [o.symbol for o in bot.paper().orders] == ["FPT"]
    market.loads.clear()
    market.analyses.clear()
    result = bot.catch_up("2026-10-12")
    assert result.sessions == ["2026-10-08", "2026-10-09", "2026-10-12"]
    assert [d for s, d in market.analyses if s == "FPT"] == result.sessions
    assert all(as_of <= "2026-10-12" for _, as_of, _ in market.loads)
    paper = bot.paper()
    buy = next(f for f in paper.fills if f.side == "BUY")
    # The order of 10-07 filled on 10-08 at 10-08's open (+ slippage), not at a later session's.
    assert buy.date == "2026-10-08" and 50_300 <= buy.price <= 50_400
    assert [h["date"] for h in paper.history] == ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"]
    assert bot.momentum_book().meta["last_day"] == "2026-10-12"
    assert "Chạy bù 2 phiên" in result.text and "BOT PAPER 2026-10-12" in result.text


@pytest.mark.unit
def test_a_long_outage_is_capped_and_stale_orders_lapse(bot, market, monkeypatch):
    monkeypatch.setattr(trader_module, "MAX_CATCH_UP", 2)
    bot.run_day("2026-10-05")
    assert bot.paper().orders[0].created == "2026-10-05"
    result = bot.catch_up("2026-10-12")
    assert result.sessions == ["2026-10-09", "2026-10-12"]
    assert "Bỏ qua 3 phiên" in result.text
    fills = bot.paper().fills
    assert not [f for f in fills if f.date == "2026-10-09"]    # the 10-05 order did not fill four sessions late
    assert [f.date for f in fills if f.side == "BUY"] == ["2026-10-12"]   # the order of 10-09 did


@pytest.mark.unit
def test_orders_whose_session_passed_unprocessed_are_dropped():
    from tradingagents.trading.ledger import Ledger, Order

    book = Ledger(cash=1e9, start_cash=1e9)
    book.orders = [Order("FPT", "BUY", 100, 50_000.0, "2026-10-05"), Order("HPG", "BUY", 100, 25_000.0, "2026-10-08")]
    stale = trader_module._expire(book, "2026-10-08")
    assert [o.symbol for o in stale] == ["FPT"] and [o.symbol for o in book.orders] == ["HPG"]
    assert trader_module._expire(book, None) == []


# ─── 4. Execution bars are the reported ones, never forward-filled ──────────


@pytest.mark.unit
def test_a_session_with_missing_prices_is_a_session_without_trading(bot, market):
    bot.run_day("2026-10-07")
    o, h, lo, c, v = market.rows[("FPT", "2026-10-08")]
    market.rows[("FPT", "2026-10-08")] = (math.nan, math.nan, math.nan, c, v)
    bot.run_day("2026-10-08")
    paper = bot.paper()
    assert not [f for f in paper.fills if f.date == "2026-10-08"]
    assert not [o for o in paper.orders if o.created == "2026-10-07"]        # the order lapsed
    assert all(not fill for s, _, fill in market.loads if s == "FPT")       # bars read unfilled


# ─── 5. Manual fills of the real account ────────────────────────────────────


@pytest.fixture
def signal_bot(bot, settings):
    object.__setattr__(settings, "trade_mode", "signal")
    object.__setattr__(settings, "signal_strategy", "momentum")
    bot.set_capital(100_000_000)
    return bot


@pytest.mark.unit
def test_reported_fills_respect_t2_cash_and_dates(signal_bot, clock):
    bot = signal_bot
    text = bot.record_fill("BUY", "FPT", 1000, 50_000)               # default: today, Vietnam time
    assert "2026-10-14" in text
    real = bot.real()
    assert real.positions["FPT"].settled_qty == 0 and real.fills[-1].date == "2026-10-14"
    before = bot.real_path.read_bytes()
    with pytest.raises(ValueError, match="T\\+2"):
        bot.record_fill("SELL", "FPT", 1000, 51_000, "2026-10-14")
    with pytest.raises(ValueError, match="tương lai"):
        bot.record_fill("SELL", "FPT", 1000, 51_000, "2026-10-16")
    with pytest.raises(ValueError, match="Không đủ tiền"):
        bot.record_fill("BUY", "HPG", 100_000, 25_000)
    with pytest.raises(ValueError, match="trước lệnh đã ghi"):
        bot.record_fill("BUY", "HPG", 100, 25_000, "2026-10-13")
    with pytest.raises(ValueError, match="cuối tuần"):
        bot.record_fill("BUY", "HPG", 100, 25_000, "2026-10-11")
    with pytest.raises(ValueError):
        bot.record_fill("BUY", "HPG", 150, 25_000)
    assert bot.real_path.read_bytes() == before                      # refused fills change nothing

    clock["now"] = _vn(2026, 10, 15, 10, 0)
    bot.record_fill("BUY", "FPT", 500, 49_000)                        # adds a lot with its own T+2 clock
    pos = bot.real().positions["FPT"]
    assert pos.qty == 1500 and pos.settled_qty == 0
    clock["now"] = _vn(2026, 10, 16, 14, 0)
    with pytest.raises(ValueError, match="T\\+2"):
        bot.record_fill("SELL", "FPT", 1500, 51_000)                  # the 10-15 lot is not in yet
    assert "lãi/lỗ" in bot.record_fill("SELL", "FPT", 1000, 51_000)  # the 10-14 lot is (T+2 afternoon)
    assert bot.real().positions["FPT"].qty == 500


@pytest.mark.unit
def test_capital_is_validated_and_a_damaged_real_book_is_never_emptied(signal_bot):
    bot = signal_bot
    for bad in (0, -5, math.nan, math.inf, 1e14):
        with pytest.raises(ValueError):
            bot.set_capital(bad)
    assert bot.real().cash == 100_000_000
    bot.real_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="hỏng"):
        bot.record_fill("BUY", "FPT", 100, 50_000)
    assert list(bot.dir.glob("real.json.corrupt-*"))                 # kept aside, not deleted
    assert "Không đọc được tài khoản thật" in bot.run_day("2026-10-14")   # paper books still run


# ─── 6. Vietnam time in stored timestamps ───────────────────────────────────


@pytest.mark.unit
def test_the_report_time_is_vietnam_time(bot, clock):
    clock["now"] = _vn(2026, 10, 14, 16, 0)
    bot.run_day("2026-10-14")
    assert bot.last_report()["at"] == "2026-10-14T16:00+07:00"


# ─── 7. One run at a time; fills are not blocked and not lost ───────────────


@pytest.mark.unit
def test_concurrent_runs_process_a_session_once(bot, market):
    market.gate = threading.Event()
    results = []
    threads = [threading.Thread(target=lambda: results.append(bot.catch_up("2026-10-14"))) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    assert bot.running
    assert "đang chạy" in bot.start_run(notify=lambda t: None)
    market.gate.set()
    for t in threads:
        t.join(10)
    assert sum(r.new for r in results) == 1
    assert len(market.analyses) == len(set(market.analyses))
    assert [h["date"] for h in bot.paper().history] == ["2026-10-14"]


@pytest.mark.unit
def test_a_reported_fill_during_a_run_is_neither_blocked_nor_overwritten(signal_bot, market):
    bot = signal_bot
    market.gate = threading.Event()
    worker = threading.Thread(target=bot.run_day, args=("2026-10-14",))
    worker.start()
    time.sleep(0.1)
    started = time.monotonic()
    bot.record_fill("BUY", "FPT", 100, 50_000)
    assert time.monotonic() - started < 2
    market.gate.set()
    worker.join(10)
    assert bot.real().positions["FPT"].qty == 100


# ─── 3. The scheduler: stamp after success, bounded retries, catch-up ───────


class _FakeBot:
    def __init__(self, failures=0, day="2026-10-13", new=True):
        self.failures, self.day, self.new = failures, day, new
        self.runs, self.calibrated = [], []

    def catch_up(self, day):
        self.runs.append(day)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("VNINDEX down")
        return trader_module.RunResult(self.day, f"report {self.day}", self.new, [self.day] if self.new else [])

    def calibrate(self, day):
        self.calibrated.append(day)
        return f"cal {day}"


@pytest.fixture
def sched_clock(monkeypatch, clock):
    t = {"now": 1000.0}
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: t["now"], sleep=time.sleep))
    return t


def _scheduler(settings, bot, notes):
    sched = DailyScheduler(settings, SimpleNamespace(submit=lambda *a, **k: None), Watchlist(settings),
                           bot=bot, notify=notes.append)
    sched._report = lambda day: None          # the watchlist run is not under test here
    return sched


@pytest.mark.unit
def test_a_failed_trading_run_is_retried_with_backoff_and_reported_once(settings, clock, sched_clock):
    notes, bot = [], _FakeBot(failures=2, day="2026-10-14")
    sched = _scheduler(settings, bot, notes)
    sched._started = True
    clock["now"] = _vn(2026, 10, 14, 15, 40)
    sched.tick()
    assert bot.runs == ["2026-10-14"] and not sched._trade_stamp.exists()
    assert len(notes) == 1 and "thử lại" in notes[0]
    sched_clock["now"] += 300
    sched.tick()
    assert len(bot.runs) == 1                                      # still waiting for the retry
    sched_clock["now"] += 301
    sched.tick()
    assert len(bot.runs) == 2 and len(notes) == 1                  # failed again: no second alert
    sched_clock["now"] += 1201
    sched.tick()
    assert len(bot.runs) == 3 and sched._trade_stamp.read_text() == "2026-10-14"
    assert notes[-2:] == ["report 2026-10-14", "cal 2026-10-14"]
    sched.tick()
    assert len(bot.runs) == 3


@pytest.mark.unit
def test_retries_are_bounded_per_day(settings, clock, sched_clock):
    notes, bot = [], _FakeBot(failures=100)
    sched = _scheduler(settings, bot, notes)
    sched._started = True
    clock["now"] = _vn(2026, 10, 14, 15, 40)
    for _ in range(20):
        sched.tick()
        sched_clock["now"] += 4000
    assert len(bot.runs) == DailyScheduler.MAX_TRADE_ATTEMPTS
    assert sched._trade_stamp.read_text() == "2026-10-14" and "tạm dừng" in notes[-1]


@pytest.mark.unit
def test_startup_catches_up_and_calibration_runs_once_per_processed_session(settings, clock, sched_clock):
    notes, bot = [], _FakeBot(day="2026-10-13")
    sched = _scheduler(settings, bot, notes)
    clock["now"] = _vn(2026, 10, 14, 10, 0)                          # a redeploy in the morning
    sched.tick()
    assert bot.runs == ["2026-10-14"] and notes == ["report 2026-10-13"]   # caught up, not stamped
    assert not sched._trade_stamp.exists() and bot.calibrated == []
    clock["now"] = _vn(2026, 10, 14, 15, 31)
    bot.day, bot.new = "2026-10-14", True
    sched.tick()
    assert bot.calibrated == ["2026-10-14"]                        # the processed session, a final one
    clock["now"] = _vn(2026, 10, 17, 15, 31)                        # Saturday: nothing new
    bot.new = False
    sched.tick()
    assert bot.calibrated == ["2026-10-14"] and notes[-1] == "cal 2026-10-14"


@pytest.mark.unit
def test_the_trading_run_waits_for_the_trade_time(settings, clock, sched_clock, monkeypatch):
    monkeypatch.setenv("DAILY_REPORT_TIME", "09:05")
    s = settings_module.load()
    notes, bot = [], _FakeBot()
    sched = _scheduler(s, bot, notes)
    sched._started = True
    clock["now"] = _vn(2026, 10, 14, 9, 10)
    assert sched._trade_due() is None and sched._due() == "2026-10-14"
    clock["now"] = _vn(2026, 10, 14, 15, 30)
    assert sched._trade_due() == "2026-10-14"


# ─── 8. Production installs what the tests run ──────────────────────────────


@pytest.mark.unit
def test_the_botkeep_requirements_pin_the_tested_pandas():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    reqs = (root / "deploy" / "botkeep" / "requirements.txt").read_text(encoding="utf-8")
    assert "pandas>=3.0.6" in reqs.splitlines()
    assert '"pandas>=3.0.6"' in (root / "pyproject.toml").read_text(encoding="utf-8")
