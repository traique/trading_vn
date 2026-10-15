"""Ledger fixes: T+2 afternoon settlement, safe loading, costed fills, add-on lots, order dedupe."""

import json
import math
from dataclasses import replace

import pytest

from tradingagents.market import vn_rules
from tradingagents.quant import params as qp
from tradingagents.trading import ledger as lg
from tradingagents.trading.ledger import Bar, Ledger, LedgerLoadError, Order, Position, Signal

pytestmark = pytest.mark.unit


def _signal(symbol="FPT", conf=0.8, entry=50_000.0, stop=47_000.0, t1=55_000.0, t2=58_000.0, size=10.0):
    return Signal(symbol, "BUY", conf, entry, entry - 500, entry, stop, t1, t2, size)


def _bought(t1=55_000.0, t2=58_000.0, stop=47_000.0) -> Ledger:
    """A book holding FPT bought on 2026-01-06 (T) at the 50.000 limit; 2026-01-07 is T+1."""
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    book.plan_orders("2026-01-05", [_signal(t1=t1, t2=t2, stop=stop)], max_positions=5)
    book.process_bar("FPT", Bar("2026-01-06", 50_000, 50_200, 49_900, 50_000, 50_000))
    assert "FPT" in book.positions
    book.process_bar("FPT", Bar("2026-01-07", 50_000, 50_200, 49_900, 50_000, 50_000))
    return book


# ─── 1. T+2: only the afternoon of T+2, modelled at the close ────────────────


def test_settlement_constants_have_one_source():
    assert lg.SETTLEMENT_SESSIONS == vn_rules.SETTLEMENT_SESSIONS == 2
    assert vn_rules.SETTLEMENT_BARS == 3          # T, T+1, T+2: what trading_graph checks


def test_t2_stop_touched_in_the_morning_but_closing_above_does_not_sell():
    book = _bought()
    assert not book.positions["FPT"].sellable
    fills = book.process_bar("FPT", Bar("2026-01-08", 46_500, 48_000, 46_000, 47_500, 50_000))
    assert fills == [] and "FPT" in book.positions                 # low under the stop, close above
    assert book.positions["FPT"].sellable and book.positions["FPT"].unsettled == []
    # T+3: the open gaps under the stop: the full rules apply, at the open less slippage.
    fills = book.process_bar("FPT", Bar("2026-01-09", 46_000, 46_500, 45_500, 46_200, 47_500))
    assert fills[0].reason == "chạm stop" and fills[0].price == 45_950


def test_t2_sell_order_fills_at_the_close_not_the_open():
    book = _bought()
    book.plan_orders("2026-01-07", [Signal("FPT", "SELL", 0.8, 50_000)], max_positions=5)
    fills = book.process_bar("FPT", Bar("2026-01-08", 52_000, 52_500, 49_000, 49_500, 50_000))
    assert len(fills) == 1 and fills[0].price == 49_450                   # 49.500 -0.1% on the 50đ grid
    assert "FPT" not in book.positions


def test_t2_target_needs_the_close_and_fills_at_the_target():
    book = _bought()
    fills = book.process_bar("FPT", Bar("2026-01-08", 53_000, 56_000, 52_500, 54_000, 50_000))
    assert fills == [] and not book.positions["FPT"].t1_done           # high over T1, close under
    book = _bought()
    fills = book.process_bar("FPT", Bar("2026-01-08", 53_000, 56_000, 52_500, 55_800, 50_000))
    assert fills[0].reason.startswith("chốt 1/2") and fills[0].price == 55_000


def test_t2_locked_at_the_floor_sells_nothing():
    book = _bought()
    book.plan_orders("2026-01-07", [Signal("FPT", "SELL", 0.8, 50_000)], max_positions=5)
    fills = book.process_bar("FPT", Bar("2026-01-08", 46_500, 46_500, 46_500, 46_500, 50_000))
    assert fills == [] and "FPT" in book.positions


def test_close_sale_proceeds_do_not_pay_for_the_same_sessions_buys():
    book = Ledger(cash=0, start_cash=10_000_000)
    book.positions["OLD"] = Position("OLD", 1000, 10_000, "2026-01-02", 0, None, None, sessions=1)
    book.orders = [Order("OLD", "SELL", 1000, None, "2026-01-05"),
                   Order("NEW", "BUY", 300, None, "2026-01-05", ref=19_000)]
    fills = book.process_session({"OLD": Bar("2026-01-06", 10_000, 10_100, 9_900, 10_000, 10_000),
                                  "NEW": Bar("2026-01-06", 19_000, 19_500, 18_800, 19_200, 19_000)})
    # OLD is on its T+2: sold at the close, too late to fund NEW at the open.
    assert [f.side for f in fills] == ["SELL"] and "NEW" not in book.positions and book.cash > 0


# ─── 3. fills: slippage, ticks, the bar's range, trade-through, bad bars ─────


def test_slipped_moves_against_the_trader_onto_the_tick_grid():
    assert vn_rules.slipped(19_000, "BUY") == 19_050               # 19.019 -> next 50đ tick
    assert vn_rules.slipped(10_000, "SELL") == 9_990               # 9.990 on the 10đ grid
    assert vn_rules.slipped(50_000, "BUY", "HNX") == 50_100
    assert vn_rules.slipped(50_000, "SELL", "UPCOM") == 49_900
    assert vn_rules.floor_to_tick(50_040) == 50_000 and vn_rules.ceil_to_tick(49_990, "HOSE") == 50_000


def test_market_fills_are_slipped_but_stay_inside_the_bar():
    book = Ledger(cash=100_000_000, start_cash=100_000_000)
    book.orders = [Order("AAA", "BUY", 100, None, "2026-01-05", ref=20_000),
                   Order("BBB", "BUY", 100, None, "2026-01-05", ref=20_000)]
    book.process_session({"AAA": Bar("2026-01-06", 20_000, 20_500, 19_800, 20_100, 20_000),
                          "BBB": Bar("2026-01-06", 20_000, 20_010, 19_800, 20_000, 20_000)})
    assert [f.price for f in book.fills] == [20_050, 20_010]       # BBB capped at its high


def test_a_limit_buy_needs_the_price_to_trade_through_it():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    book.plan_orders("2026-01-05", [_signal()], max_positions=5)
    book.process_bar("FPT", Bar("2026-01-06", 50_500, 51_000, 50_000, 50_800, 50_000))   # low == limit
    assert "FPT" not in book.positions
    book.plan_orders("2026-01-06", [_signal()], max_positions=5)
    book.process_bar("FPT", Bar("2026-01-07", 50_500, 51_000, 49_950, 50_800, 50_800))   # through it
    assert book.fills[-1].price == 50_000                                                 # at the limit
    book2 = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    book2.plan_orders("2026-01-05", [_signal()], max_positions=5)
    book2.process_bar("FPT", Bar("2026-01-06", 49_960, 50_400, 49_900, 50_200, 50_000))
    assert book2.fills[-1].price == 50_000                     # open+slippage capped at the limit


@pytest.mark.parametrize("bad", [
    Bar("2026-01-09", math.nan, math.nan, math.nan, math.nan, 50_000),
    Bar("2026-01-09", 0, 0, 0, 0, 50_000),
    Bar("2026-01-09", 40_000, 41_000, 39_000, float("inf"), 50_000),
    Bar("2026-01-09", 40_000, 39_000, 41_000, 40_000, 50_000),       # low above high
    Bar("2026-01-09", 40_000, 41_000, 39_000, 40_000, 50_000, volume=0),
])
def test_a_bar_without_believable_prices_is_a_session_without_trading(bad):
    book = _bought()
    book.process_bar("FPT", Bar("2026-01-08", 50_000, 50_200, 49_900, 50_000, 50_000))   # T+2
    pos = book.positions["FPT"]
    sessions, last = pos.sessions, pos.last_price
    book.plan_orders("2026-01-08", [Signal("FPT", "SELL", 0.8, 50_000)], max_positions=5)
    assert book.process_bar("FPT", bad) == []
    assert "FPT" in book.positions and pos.sessions == sessions and pos.last_price == last
    assert math.isfinite(book.nav())
    book.end_of_session("2026-01-09", math.nan)
    assert book.history[-1]["benchmark"] is None


# ─── 4. adding to a holding, and fills reported by hand ──────────────────────


def test_shares_added_to_a_holding_settle_on_their_own_clock():
    book = _bought()
    book.process_bar("FPT", Bar("2026-01-08", 50_000, 50_200, 49_900, 50_000, 50_000))   # first lot settles
    pos = book.positions["FPT"]
    first, cost = pos.qty, pos.qty * pos.avg_price
    book.record_fill("BUY", "FPT", 1000, 51_000, "2026-01-08")
    assert pos.qty == first + 1000 and pos.settled_qty == first
    assert pos.avg_price == pytest.approx((cost + 51_000_000 * 1.0015) / (first + 1000))
    with pytest.raises(ValueError, match="T\\+2"):
        book.record_fill("SELL", "FPT", first + 1000, 52_000, "2026-01-09")
    # A SELL order on 01-09 (T+1 of the new lot) sells only the settled shares.
    book.plan_orders("2026-01-08", [Signal("FPT", "SELL", 0.8, 50_000)], max_positions=5)
    fills = book.process_bar("FPT", Bar("2026-01-09", 50_000, 50_200, 49_900, 50_000, 50_000))
    assert fills[0].qty == first and book.positions["FPT"].qty == 1000
    assert not book.positions["FPT"].sellable


def test_record_fill_validates_lots_cash_and_settlement():
    book = Ledger(cash=60_000_000, start_cash=60_000_000)
    for bad in (0, -100, 150, 99):
        with pytest.raises(ValueError, match="bội số"):
            book.record_fill("BUY", "FPT", bad, 50_000, "2026-10-14")
    with pytest.raises(ValueError, match="Không đủ tiền"):
        book.record_fill("BUY", "FPT", 2000, 50_000, "2026-10-14")
    with pytest.raises(ValueError, match="Giá"):
        book.record_fill("BUY", "FPT", 100, math.nan, "2026-10-14")
    fill = book.record_fill("buy", "fpt", 1000, 50_000, "2026-10-14", stop=47_000, target1=55_000)
    assert fill.cost == 75_000 and book.cash == pytest.approx(60_000_000 - 50_075_000)
    assert book.positions["FPT"].stop == 47_000 and not book.positions["FPT"].sellable
    with pytest.raises(ValueError, match="T\\+2"):
        book.record_fill("SELL", "FPT", 1000, 51_000, "2026-10-15")            # T+1
    with pytest.raises(ValueError, match="không có đủ"):
        book.record_fill("SELL", "FPT", 2000, 51_000, "2026-10-16")
    sell = book.record_fill("SELL", "FPT", 1000, 51_000, "2026-10-16")         # T+2 afternoon
    assert sell.pnl == round(51_000_000 * (1 - 0.0025) - 50_075_000) and "FPT" not in book.positions


def test_record_fill_counts_weekdays_for_settlement():
    book = Ledger(cash=100_000_000, start_cash=100_000_000)
    book.record_fill("BUY", "FPT", 100, 50_000, "2026-10-15")                  # Thursday
    with pytest.raises(ValueError):
        book.record_fill("SELL", "FPT", 100, 50_000, "2026-10-18")             # Sunday: T+1 only
    book.record_fill("SELL", "FPT", 100, 50_000, "2026-10-19")                 # Monday = T+2


# ─── 2. loading and saving ───────────────────────────────────────────────────


def test_a_missing_file_starts_fresh(tmp_path):
    book = Ledger.load(tmp_path / "none.json", 5_000_000)
    assert book.cash == 5_000_000 and not book.positions


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2]", '{"start_cash": 1}', '{"cash": NaN}',
                                     '{"cash": 1, "positions": {"FPT": {"symbol": "FPT"}}}'])
def test_a_damaged_book_is_moved_aside_and_never_silently_replaced(tmp_path, content):
    path = tmp_path / "real.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(LedgerLoadError, match="corrupt"):
        Ledger.load(path, 0.0)
    assert not path.exists()
    aside = list(tmp_path.glob("real.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == content


def test_fresh_on_error_is_an_explicit_opt_in(tmp_path):
    path = tmp_path / "paper.json"
    path.write_text("{oops", encoding="utf-8")
    book = Ledger.load(path, 7_000_000, fresh_on_error=True)
    assert book.cash == 7_000_000 and list(tmp_path.glob("paper.json.corrupt-*"))


def test_an_unreadable_file_is_left_in_place(tmp_path):
    path = tmp_path / "real.json"
    path.mkdir()                                         # reading a directory: OSError
    with pytest.raises(LedgerLoadError):
        Ledger.load(path, 0.0, fresh_on_error=True)
    assert path.exists()


def test_schema_changes_are_not_corruption(tmp_path):
    path = tmp_path / "real.json"
    old = {"cash": 1_000.0, "start_cash": 2_000.0, "future_field": 1,
           "positions": {"FPT": {"symbol": "FPT", "qty": 100, "avg_price": 50_000, "buy_date": "2026-01-06",
                                 "stop": 47_000, "target1": None, "target2": None, "sessions": 0,
                                 "t1_done": False, "last_price": 50_000, "peak": 50_000, "new_thing": "x"}},
           "orders": [{"symbol": "FPT", "side": "SELL", "qty": 100, "limit": None, "created": "2026-01-07",
                       "extra": True}],
           "fills": [{"date": "2026-01-06", "symbol": "FPT", "side": "BUY", "qty": 100, "price": 50_000,
                      "cost": 75, "reason": "x"}]}
    path.write_text(json.dumps(old), encoding="utf-8")
    book = Ledger.load(path, 0.0)
    pos = book.positions["FPT"]
    assert book.cash == 1_000 and pos.qty == 100 and not pos.sellable        # old save: sessions 0, unsettled
    assert book.orders[0].side == "SELL" and book.fills[0].pnl is None
    settled = Ledger.from_dict({"cash": 1, "positions": {"A": {"symbol": "A", "qty": 100, "avg_price": 1,
                                                               "buy_date": "2026-01-01", "sessions": 2}}})
    assert settled.positions["A"].sellable and settled.start_cash == 1


def test_save_is_atomic_and_round_trips_unsettled_lots(tmp_path):
    path = tmp_path / "books" / "real.json"
    book = Ledger(cash=100_000_000, start_cash=100_000_000)
    book.record_fill("BUY", "FPT", 100, 50_000, "2026-10-14")
    book.save(path)
    book.save(path)
    assert sorted(p.name for p in path.parent.iterdir()) == ["real.json"]
    again = Ledger.load(path, 0.0)
    assert again.positions["FPT"].unsettled == [{"date": "2026-10-14", "qty": 100, "sessions": 0}]


# ─── 5. no duplicate orders ──────────────────────────────────────────────────


def test_planning_a_session_twice_does_not_double_the_orders():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    first = book.plan_orders("2026-01-05", [_signal(), _signal(conf=0.7)], max_positions=5)
    again = book.plan_orders("2026-01-05", [_signal()], max_positions=5)
    assert len(first) == 1 and again == [] and len(book.orders) == 1
    book = _bought()
    sells = [Signal("FPT", "SELL", 0.8, 50_000)]
    assert len(book.plan_orders("2026-01-07", sells, max_positions=5)) == 1
    assert book.plan_orders("2026-01-07", sells + [_signal()], max_positions=5) == []   # held: no BUY
    assert [o.side for o in book.orders] == ["SELL"]


def test_signals_with_nan_levels_are_not_planned():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    assert book.plan_orders("2026-01-05", [_signal(entry=math.nan), _signal("AAA", size=math.nan)],
                            max_positions=5) == []


# ─── 6. momentum shares the ledger's rules ───────────────────────────────────


def test_momentum_exits_follow_t2_and_slippage_and_orders_are_not_duplicated():
    from tradingagents.trading import momentum as mom

    p = replace(qp.DEFAULT, mom_top_k=1, mom_lookback=5, mom_market_ma=10, mom_rebalance=3)
    book = Ledger(cash=100_000_000, start_cash=100_000_000)
    decision = mom.Decision(True, True, ["AAA"])
    mom.plan(book, "2026-01-05", decision, {"AAA": 20_000}, p)
    assert mom.plan(book, "2026-01-05", decision, {"AAA": 20_000}, p) == []         # planned twice
    book.process_session({"AAA": Bar("2026-01-06", 20_000, 20_400, 19_900, 20_200, 20_000)}, rules=mom.RULES)
    assert book.fills[-1].price == 20_050                                          # open + slippage
    book.process_session({"AAA": Bar("2026-01-07", 20_200, 20_400, 19_900, 20_200, 20_200)}, rules=mom.RULES)
    out = mom.Decision(False, False, [])
    mom.plan(book, "2026-01-07", out, {"AAA": 20_200}, p)
    assert len(mom.plan(book, "2026-01-07", out, {"AAA": 20_200}, p)) == 0
    fills = book.process_session({"AAA": Bar("2026-01-08", 21_000, 21_500, 19_000, 19_500, 20_200)},
                                 rules=mom.RULES)
    assert fills[0].side == "SELL" and fills[0].price == 19_450                    # T+2: at the close
