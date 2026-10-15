# ruff: noqa: F811 - pytest fixtures imported from test_bot_runtime_fixes
"""Regression tests for the review round: the real book after T1, T+2 in the real
advice, corporate actions in the paper books, a late VNINDEX bar, and a price
source outage in the middle of a run.

No network: prices, the VN30 basket, the listing and the engine are stubbed;
the clock (Vietnam time) is fixed per test.
"""

import pytest

from tests.test_bot_runtime_fixes import (  # noqa: F401 - shared fixtures
    _FakeBot,
    _scheduler,
    _vn,
    bot,
    clock,
    market,
    sched_clock,
    settings,
)
from tradingagents.market import vn_rules
from tradingagents.quant import params as qp
from tradingagents.trading.ledger import Bar, Ledger, Position, Signal
from vnbot import service
from vnbot.service import DailyScheduler

pytestmark = pytest.mark.unit


# ─── 1. The real book after the owner takes T1 ───────────────────────────────


def _real_with_fpt(day="2026-10-12", price=50_000, stop=47_000.0, t1=55_000.0) -> Ledger:
    real = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    real.record_fill("BUY", "FPT", 1000, price, day, stop=stop, target1=t1, target2=58_000.0)
    return real


def test_a_part_sold_at_t1_marks_t1_done_and_moves_the_stop_to_break_even():
    real = _real_with_fpt()
    real.record_fill("SELL", "FPT", 500, 55_000, "2026-10-14")
    pos = real.positions["FPT"]
    assert pos.qty == 500 and pos.t1_done
    assert pos.stop == pytest.approx(pos.avg_price) and pos.stop > 50_000


def test_a_part_sold_below_t1_changes_no_level():
    real = _real_with_fpt()
    real.record_fill("SELL", "FPT", 500, 52_000, "2026-10-14")
    pos = real.positions["FPT"]
    assert not pos.t1_done and pos.stop == 47_000.0


def test_the_t1_advice_is_not_repeated_after_the_owner_took_t1(bot, settings):
    real = _real_with_fpt()
    real.record_fill("SELL", "FPT", 500, 55_000, "2026-10-14")
    bars = {"FPT": Bar("2026-10-15", 55_000, 55_500, 54_800, 55_200, 55_000)}
    text = bot._real_report("2026-10-15", real, bars, [], {}, qp.DEFAULT)
    assert "chạm T1" not in text
    # Under break-even now, the stop alert fires (it would not at the old 47.000).
    bars = {"FPT": Bar("2026-10-16", 50_500, 50_600, 49_900, 50_000, 55_200)}
    assert "chạm stop" in bot._real_report("2026-10-16", real, bars, [], {}, qp.DEFAULT)


# ─── 2. T+2 in the real-account advice ───────────────────────────────────────


def _bought_monday() -> Ledger:
    real = Ledger(cash=500_000_000, start_cash=500_000_000)
    real.record_fill("BUY", "FPT", 1000, 100_000, "2026-10-12", stop=95_000.0, target1=110_000.0)
    return real


def test_a_stop_on_the_buy_day_does_not_advise_selling_shares_not_yet_arrived(bot):
    real = _bought_monday()
    real.settle_to("2026-10-12")
    bars = {"FPT": Bar("2026-10-12", 100_000, 100_500, 94_000, 96_000, 100_000)}
    text = bot._real_report("2026-10-12", real, bars, [], {}, qp.DEFAULT)
    assert "chạm stop" in text and "đề xuất BÁN 1.000 cp" not in text
    assert "1.000 cp chưa về đủ T+2: chỉ bán được từ chiều 2026-10-14" in text and "ATC" in text


def test_on_t_plus_1_the_next_session_is_the_t2_afternoon(bot):
    real = _bought_monday()
    real.settle_to("2026-10-13")
    bars = {"FPT": Bar("2026-10-13", 96_000, 96_500, 94_000, 95_000, 96_000)}
    text = bot._real_report("2026-10-13", real, bars, [], {}, qp.DEFAULT)
    assert "chiều phiên tới (2026-10-14)" in text


def test_settled_shares_are_advised_in_full(bot):
    real = _bought_monday()
    real.settle_to("2026-10-14")
    bars = {"FPT": Bar("2026-10-14", 96_000, 96_500, 94_000, 95_000, 96_000)}
    text = bot._real_report("2026-10-14", real, bars, [], {}, qp.DEFAULT)
    assert "đề xuất BÁN 1.000 cp" in text and "T+2" not in text


def test_a_sell_signal_splits_settled_and_unsettled_shares(bot):
    real = _bought_monday()
    real.record_fill("BUY", "FPT", 500, 100_000, "2026-10-14")
    real.settle_to("2026-10-14")
    sell = Signal("FPT", "SELL", 0.7, 100_000)
    bars = {"FPT": Bar("2026-10-14", 100_000, 101_000, 99_000, 100_000, 100_000)}
    text = bot._real_report("2026-10-14", real, bars, [sell], {}, qp.DEFAULT)
    assert "đề xuất bán 1.000 cp phiên tới" in text and "500 cp chưa về đủ T+2" in text


def test_t1_needs_the_session_to_trade_through_it_as_in_the_ledger(bot):
    real = _bought_monday()
    real.settle_to("2026-10-15")
    touch = {"FPT": Bar("2026-10-15", 105_000, 110_000, 104_000, 108_000, 104_000)}
    through = {"FPT": Bar("2026-10-15", 105_000, 110_100, 104_000, 108_000, 104_000)}
    assert "chạm T1" not in bot._real_report("2026-10-15", real, touch, [], {}, qp.DEFAULT)
    assert "chạm T1" in bot._real_report("2026-10-15", real, through, [], {}, qp.DEFAULT)


def test_the_fill_reply_says_t1_was_taken(bot, settings, clock):
    object.__setattr__(settings, "trade_mode", "signal")
    clock["now"] = _vn(2026, 10, 14, 16, 0)
    bot.set_capital(500_000_000)
    with bot._real_lock:
        real = bot.real()
        real.record_fill("BUY", "FPT", 1000, 50_000, "2026-10-12", stop=47_000.0, target1=55_000.0)
        real.save(bot.real_path)
    assert "Đã chốt T1" in bot.record_fill("SELL", "FPT", 500, 55_100, "2026-10-14")


# ─── 3. Corporate actions in the paper books ─────────────────────────────────


def _held(last_date="2026-10-12") -> Ledger:
    book = Ledger(cash=100_000_000, start_cash=400_000_000)
    book.positions["FPT"] = Position("FPT", 10_000, 30_000.0, "2026-10-01", 28_000.0, 33_000.0, 36_000.0,
                                     sessions=8, last_price=30_000.0, peak=30_500.0, unsettled=[],
                                     last_date=last_date)
    return book


EX_DATE = Bar("2026-10-13", 25_000, 25_300, 24_900, 25_100, 25_000, prev_date="2026-10-12")


def test_a_bonus_issue_rebases_the_position_instead_of_tripping_the_stop():
    # Regression: the ex-date drop (20% bonus shares) sold everything at a fake -17%.
    book = _held()
    nav_before = book.nav()
    fills = book.process_session({"FPT": EX_DATE}, params=qp.DEFAULT)
    assert fills == []
    pos = book.positions["FPT"]
    assert pos.qty == 12_000 and pos.avg_price == pytest.approx(25_000.0)
    assert pos.stop == pytest.approx(vn_rules.round_to_tick(28_000 / 1.2)) and pos.target1 == pytest.approx(27_500.0)
    assert pos.target2 == pytest.approx(30_000.0) and pos.last_date == "2026-10-13"
    assert book.nav() == pytest.approx(book.cash + (nav_before - book.cash) * 25_100 / 25_000)   # no fake loss
    assert book.meta["adjustments"][-1]["after"] == 12_000


def test_no_rebase_when_the_previous_close_is_not_the_session_the_book_marked():
    book = _held(last_date="2026-10-09")          # a gap: the two closes cannot be compared
    fills = book.process_session({"FPT": EX_DATE}, params=qp.DEFAULT)
    assert [f.reason for f in fills] == ["chạm stop"]


def test_an_ordinary_session_changes_nothing():
    book = _held()
    bar = Bar("2026-10-13", 30_000, 30_300, 29_800, 30_100, 30_000, prev_date="2026-10-12")
    assert book.process_session({"FPT": bar}, params=qp.DEFAULT) == []
    assert book.positions["FPT"].qty == 10_000 and "adjustments" not in book.meta


def test_a_buy_order_planned_before_the_ex_date_is_rebased():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    sig = Signal("FPT", "BUY", 0.8, 30_000, 29_500, 30_000, 28_000, 33_000, 36_000, 10)
    book.plan_orders("2026-10-12", [sig], max_positions=5)
    book.process_session({"FPT": EX_DATE}, params=qp.DEFAULT)
    pos = book.positions["FPT"]
    assert pos.stop == pytest.approx(vn_rules.round_to_tick(28_000 / 1.2)) and pos.target1 == pytest.approx(27_500.0)


def test_the_bot_report_mentions_the_adjustment(bot, market):
    bot.run_day("2026-10-07")
    bot.run_day("2026-10-08")                          # FPT bought on 10-08
    assert "FPT" in bot.paper().positions
    # 10-09 is an ex-date: the source re-prices every earlier close by 1/1.2.
    for (s, d), v in list(market.rows.items()):
        if s == "FPT" and d <= "2026-10-08":
            market.rows[(s, d)] = tuple(x / 1.2 for x in v[:4]) + (v[4],)
    market.rows[("FPT", "2026-10-09")] = (42_000, 42_300, 41_800, 42_100, 1e6)
    report = bot.run_day("2026-10-09")
    assert "giá điều chỉnh quyền" in report
    assert not [f for f in bot.paper().fills if f.side == "SELL"]


# ─── 4. A price source outage in the middle of a run ─────────────────────────


def test_a_session_is_not_saved_when_the_sources_do_not_answer(bot, market):
    from tradingagents.dataflows.errors import VendorUnavailableError
    from tradingagents.quant import engine

    bot.run_day("2026-10-07")
    analyze = engine.analyze

    def flaky(symbol, day, portfolio=None, params=None):
        if symbol == "HPG":
            raise VendorUnavailableError("dnse failed recently; not asked again yet")
        return analyze(symbol, day, portfolio=portfolio, params=params)

    engine.analyze = flaky
    try:
        with pytest.raises(RuntimeError, match="chưa lưu"):
            bot.catch_up("2026-10-08")
    finally:
        engine.analyze = analyze
    assert bot.paper().meta["last_day"] == "2026-10-07"      # retried later, not saved half done
    assert bot.catch_up("2026-10-08").sessions == ["2026-10-08"]


def test_a_stock_without_data_does_not_stop_the_session(bot, market):
    from tradingagents.quant import engine

    analyze = engine.analyze

    def no_data(symbol, day, portfolio=None, params=None):
        if symbol == "HPG":
            raise ValueError("no closed session")
        return analyze(symbol, day, portfolio=portfolio, params=params)

    engine.analyze = no_data
    try:
        result = bot.catch_up("2026-10-07")
    finally:
        engine.analyze = analyze
    assert result.sessions == ["2026-10-07"] and "1 mã lỗi dữ liệu" in result.text


# ─── 5. A late VNINDEX bar is not a holiday ──────────────────────────────────


def test_a_late_bar_is_retried_not_stamped_as_done(settings, clock, sched_clock):
    notes, bot_ = [], _FakeBot(day="2026-10-12", new=False)
    sched = _scheduler(settings, bot_, notes)
    sched._started = True
    clock["now"] = _vn(2026, 10, 13, 15, 31)                        # Tuesday; only Monday's bar so far
    sched.tick()
    assert bot_.runs == ["2026-10-13"] and not sched._trade_stamp.exists()
    sched_clock["now"] += 300
    sched.tick()
    assert len(bot_.runs) == 1                                        # waiting
    sched_clock["now"] += DailyScheduler.LATE_BAR_RETRY_SECONDS
    bot_.day, bot_.new = "2026-10-13", True                           # the bar is out
    sched.tick()
    assert len(bot_.runs) == 2 and sched._trade_stamp.read_text() == "2026-10-13"
    assert notes == ["report 2026-10-13", "cal 2026-10-13"]


def test_a_weekday_without_a_bar_by_the_cutoff_is_a_holiday(settings, clock, sched_clock):
    notes, bot_ = [], _FakeBot(day="2026-10-12", new=False)
    sched = _scheduler(settings, bot_, notes)
    sched._started = True
    clock["now"] = _vn(2026, 10, 13, 18, 1)
    sched.tick()
    assert sched._trade_stamp.read_text() == "2026-10-13"


def test_the_watchlist_run_waits_for_a_late_bar(settings, clock, sched_clock, monkeypatch):
    submitted = []
    sched = DailyScheduler(settings, type("Q", (), {"submit": lambda self, *a, **k: submitted.append(a)})(),
                           service.Watchlist(settings))
    monkeypatch.setattr(DailyScheduler, "_is_trading_day", staticmethod(lambda day: False))
    clock["now"] = _vn(2026, 10, 13, 15, 40)
    sched._report("2026-10-13")
    assert not sched._stamp.exists() and sched._next_try > sched_clock["now"]
    clock["now"] = _vn(2026, 10, 13, 18, 5)
    sched._report("2026-10-13")
    assert sched._stamp.read_text() == "2026-10-13" and submitted == []
