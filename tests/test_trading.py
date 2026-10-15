"""The trading layer: ledger rules, money-flow features, params, policy flow effects, calibration."""

from dataclasses import replace

import pandas as pd
import pytest

from tradingagents.market import vn_rules
from tradingagents.quant import flow_features as ff, params as qp, policy, price_adjust
from tradingagents.trading import calibrate as cal, simulator as sim
from tradingagents.trading.ledger import Bar, Ledger, Signal


def _buy_signal(symbol="FPT", conf=0.8, entry=50_000.0, stop=47_000.0, t1=55_000.0, t2=58_000.0, size=10.0):
    return Signal(symbol, "BUY", conf, entry, entry - 500, entry, stop, t1, t2, size)


# ─── Ledger ──────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_buy_fills_next_session_in_lots_and_cannot_be_sold_before_t_plus_2():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    orders = book.plan_orders("2026-01-05", [_buy_signal()], max_positions=5)
    assert orders[0].qty % 100 == 0 and orders[0].limit == 50_000
    book.process_bar("FPT", Bar("2026-01-06", 49_800, 50_500, 49_500, 50_200, 50_000))
    pos = book.positions["FPT"]
    assert pos.avg_price > 49_800 and pos.qty == orders[0].qty      # filled at the open, fee in cost
    # T+1 touches the stop: not sellable yet.
    book.process_bar("FPT", Bar("2026-01-07", 48_000, 48_500, 46_000, 46_500, 50_200))
    assert "FPT" in book.positions
    # T+2: sellable, the stop (gapped below) fills at the open.
    fills = book.process_bar("FPT", Bar("2026-01-08", 45_000, 46_000, 44_500, 45_500, 46_500))
    assert fills[0].side == "SELL" and fills[0].price == 45_000 and fills[0].pnl < 0
    assert "FPT" not in book.positions


@pytest.mark.unit
def test_t1_sells_half_and_moves_the_stop_to_break_even_then_t2_the_rest():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    book.plan_orders("2026-01-05", [_buy_signal()], max_positions=5)
    book.process_bar("FPT", Bar("2026-01-06", 50_000, 50_000, 49_900, 50_000, 50_000))
    qty = book.positions["FPT"].qty
    book.process_bar("FPT", Bar("2026-01-07", 50_100, 51_000, 50_000, 50_800, 50_000))
    fills = book.process_bar("FPT", Bar("2026-01-08", 54_000, 55_500, 53_800, 55_200, 50_800))
    assert fills[0].reason.startswith("chốt 1/2") and fills[0].price == 55_000
    pos = book.positions["FPT"]
    assert pos.qty == qty - fills[0].qty and pos.t1_done and pos.stop >= pos.avg_price
    fills = book.process_bar("FPT", Bar("2026-01-09", 56_000, 58_500, 55_900, 58_000, 55_200))
    assert fills[-1].price == 58_000 and "FPT" not in book.positions


@pytest.mark.unit
def test_limit_locked_sessions_fill_nothing():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    book.plan_orders("2026-01-05", [_buy_signal()], max_positions=5)
    book.process_bar("FPT", Bar("2026-01-06", 53_500, 53_500, 53_500, 53_500, 50_000))   # ceiling, one price
    assert "FPT" not in book.positions and not book.orders                               # lapsed after its session


@pytest.mark.unit
def test_orders_respect_slots_cash_and_rank_by_confidence():
    book = Ledger(cash=100_000_000, start_cash=100_000_000)
    sigs = [_buy_signal("AAA", 0.80, size=20), _buy_signal("BBB", 0.90, size=20), _buy_signal("CCC", 0.85, size=20)]
    orders = book.plan_orders("2026-01-05", sigs, max_positions=2)
    assert [o.symbol for o in orders] == ["BBB", "CCC"]
    assert sum(o.qty * o.limit for o in orders) <= book.cash


@pytest.mark.unit
def test_a_sell_signal_exits_at_the_next_open_and_the_book_round_trips(tmp_path):
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    book.plan_orders("2026-01-05", [_buy_signal()], max_positions=5)
    for day, prev in (("2026-01-06", 50_000), ("2026-01-07", 50_000)):
        book.process_bar("FPT", Bar(day, 50_000, 50_200, 49_900, 50_000, prev))
    book.plan_orders("2026-01-07", [Signal("FPT", "SELL", 0.8, 50_000)], max_positions=5)
    path = tmp_path / "book.json"
    book.save(path)
    again = Ledger.load(path, 0)
    fills = again.process_bar("FPT", Bar("2026-01-08", 49_500, 50_000, 49_000, 49_800, 50_000))
    assert fills[0].price == 49_500 and fills[0].reason == "hệ thống chuyển SELL"


@pytest.mark.unit
def test_the_time_stop_closes_an_old_position():
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    book.plan_orders("2026-01-05", [_buy_signal()], max_positions=5)
    book.process_bar("FPT", Bar("2026-01-06", 50_000, 50_100, 49_900, 50_000, 50_000))
    fills = []
    for i in range(1, 5):
        fills += book.process_bar("FPT", Bar(f"2026-01-{6 + i:02d}", 50_000, 50_100, 49_900, 50_050, 50_000),
                                  max_hold_sessions=4)
    assert fills and fills[-1].reason.startswith("hết 4 phiên")


# ─── Money-flow features ─────────────────────────────────────────────────────


def _flow_frame(nets, match=100e9, last="2026-10-13", prop=None, room=20.0):
    dates = pd.bdate_range(end=last, periods=len(nets))
    return pd.DataFrame({
        "Date": dates, "ForeignNet": nets, "MatchValue": [match] * len(nets),
        "ForeignRoomPct": [room] * len(nets), "PropNet": prop if prop is not None else [float("nan")] * len(nets),
    })


@pytest.mark.unit
def test_stock_flow_features_measure_streaks_ratios_and_staleness():
    f = ff.stock_flows(_flow_frame([-1e9] * 14 + [2e9] * 6), "2026-10-13")
    assert f.foreign_streak == 6 and f.foreign_sell_days_10 == 4
    assert f.foreign_ratio_20 == pytest.approx((-14 + 12) / 2000 * 100)
    assert ff.stock_flows(_flow_frame([1e9] * 20, last="2026-09-01"), "2026-10-13") is None   # stale


@pytest.mark.unit
def test_market_flow_features_and_breadth_divergence():
    flows = pd.DataFrame({"Date": pd.bdate_range(end="2026-10-13", periods=20),
                          "ForeignNet": [-1e11] * 20, "PropNet": [1e10] * 20})
    breadth = pd.DataFrame({"Date": pd.bdate_range(end="2026-10-13", periods=15),
                            "Advances": [100] * 15, "Declines": [200] * 15,
                            "PctAboveMA50": [60.0] * 4 + [50.0] * 11, "NewHighs20": [3] * 15})
    vni = [1000.0 + i for i in range(30)]                     # at a 20-session high
    m = ff.market_flows(flows, None, breadth, vni, "2026-10-13")
    assert m.foreign_sell_days_15 == 15 and m.foreign_net_20 == -2e12
    assert m.adv_ratio_10 == pytest.approx(33.3, abs=0.1) and m.breadth_divergence


# ─── Params ──────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_params_default_to_the_policy_constants_and_saved_ones_are_clamped(tmp_path, monkeypatch):
    assert qp.DEFAULT.confidence_buy_min == policy.CONFIDENCE_BUY_MIN
    assert qp.DEFAULT.min_rr_ratio == policy.MIN_RR_RATIO
    assert qp.DEFAULT.distribution_day_threshold == policy.DISTRIBUTION_DAY_THRESHOLD
    monkeypatch.setenv("TRADINGAGENTS_PARAMS_PATH", str(tmp_path / "p.json"))
    assert qp.active() == qp.DEFAULT
    qp.save(replace(qp.DEFAULT, confidence_buy_min=0.7, flow_weight=0.1))
    assert qp.active().confidence_buy_min == 0.7
    assert qp.coerce({"confidence_buy_min": 0.1, "unknown": 1}).confidence_buy_min == 0.60
    assert qp.coerce({"distribution_day_threshold": "5"}).distribution_day_threshold == 5


# ─── Policy: flows move confidence and the regime ────────────────────────────


@pytest.fixture
def bullish_inputs():
    from tests.test_quant_policy import _closes_uptrend, _make_inputs

    return _make_inputs(_closes_uptrend())


def _stock(streak=6, ratio=8.0, sell10=0, room=20.0, prop=None):
    return ff.StockFlows(20, 1e11, ratio, streak, sell10, room, prop, "2026-10-13")


def _market(sell15=0, net=1e11, div=False, etf=None):
    return ff.MarketFlows(net, sell15, None, etf, 55.0, 60.0, 0.0, 5, div)


@pytest.mark.unit
def test_foreign_accumulation_lifts_and_distribution_lowers_a_bullish_confidence(bullish_inputs):
    base = policy.evaluate_policy(bullish_inputs).confidence
    params = replace(qp.DEFAULT, flow_weight=0.1)
    up = policy.evaluate_policy(replace(bullish_inputs, stock_flows=_stock(), params=params))
    down = policy.evaluate_policy(replace(bullish_inputs, stock_flows=_stock(streak=-8, ratio=-8, sell10=8), params=params))
    assert up.confidence > base > down.confidence
    assert any("khối ngoại mua ròng" in r for r in up.reasons)
    full = policy.evaluate_policy(replace(bullish_inputs, stock_flows=_stock(room=0.5), params=params))
    assert any("room khối ngoại gần hết" in r for r in full.reasons)


@pytest.mark.unit
def test_zero_flow_weight_reproduces_the_original_decision(bullish_inputs):
    off = replace(qp.DEFAULT, flow_weight=0.0)
    plain = policy.evaluate_policy(bullish_inputs)
    with_flows = policy.evaluate_policy(replace(bullish_inputs, stock_flows=_stock(), params=off))
    assert (plain.action, plain.confidence) == (with_flows.action, with_flows.confidence)


@pytest.mark.unit
def test_market_foreign_selling_and_breadth_divergence_lower_the_regime(bullish_inputs):
    regime = policy.evaluate_policy(bullish_inputs).market_regime
    selling = policy.evaluate_policy(replace(bullish_inputs, market_flows=_market(sell15=13, net=-5e12)))
    order = ["risk_off", "neutral", "risk_on"]
    assert regime == "risk_on"
    assert order.index(selling.market_regime) == order.index(regime) - 1
    both = policy.evaluate_policy(replace(bullish_inputs, market_flows=_market(sell15=13, net=-5e12, div=True)))
    assert both.market_regime == "risk_off"
    off = replace(qp.DEFAULT, market_foreign_sell_days=15, breadth_divergence_on=0)
    assert policy.evaluate_policy(replace(bullish_inputs, market_flows=_market(sell15=13, net=-5e12, div=True),
                                          params=off)).market_regime == regime


@pytest.mark.unit
def test_thresholds_come_from_params(bullish_inputs):
    strict = replace(qp.DEFAULT, confidence_buy_min=0.99, confidence_watch_min=0.98)
    assert policy.evaluate_policy(replace(bullish_inputs, params=strict)).action in ("NO_TRADE", "WATCH")


# ─── Fixes: gaps inside the band, penny bands ────────────────────────────────


@pytest.mark.unit
def test_a_move_inside_the_exchange_band_is_not_an_unadjusted_gap():
    closes = [10_000, 11_400, 11_400]                        # +14% on UPCoM: a real move
    assert price_adjust.detect_price_gaps(closes, exchange="UPCOM") == []
    assert price_adjust.detect_price_gaps(closes, exchange=None)          # unknown exchange: flagged as before
    up_gap = price_adjust.detect_price_gaps([10_000, 13_000], exchange="HOSE")
    assert price_adjust.last_gap_in_window(up_gap, 2) is None               # a rise is never ex-rights
    bonus = price_adjust.detect_price_gaps([12_000, 10_000], exchange="UPCOM")   # -16,67% = 1:5 bonus
    assert bonus and bonus[0].ratio_hint


@pytest.mark.unit
def test_penny_prices_still_have_a_one_tick_band():
    assert vn_rules.ceiling_floor(500, "UPCOM") == (600.0, 400.0)
    assert vn_rules.ceiling_floor(56_300, "HOSE") == (60_200.0, 52_400.0)


# ─── Simulator and calibration on synthetic data ─────────────────────────────


def _dataset(n=120):
    from tests.test_quant_policy import _closes_uptrend, _make_inputs

    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-01", periods=n)]
    data = sim.Dataset(dates=dates, symbols=["AAA"])
    base = _make_inputs(_closes_uptrend())
    price = base.price
    bars, prev = {}, None
    for i, day in enumerate(dates):
        close = price * (1 + 0.002 * i)
        bars[day] = Bar(day, close * 0.998, close * 1.01, close * 0.99, close, prev)
        prev = close
        data.inputs[("AAA", day)] = base
        data.benchmark[day] = 1000 + i
    data.bars["AAA"] = bars
    return data


@pytest.mark.unit
def test_the_simulator_trades_the_policy_decisions_and_scores_them():
    data = _dataset()
    ledger = sim.run(data, qp.DEFAULT)
    s = sim.score(ledger)
    assert s.value == pytest.approx(s.return_pct - 0.5 * s.max_drawdown_pct)
    assert len(ledger.history) == len(data.dates)


@pytest.mark.unit
def test_calibration_changes_one_step_at_most_and_records_every_night(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_PARAMS_PATH", str(tmp_path / "quant_params.json"))
    data = _dataset()
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.champion.train is not None and report.tried
    for trial in report.tried:
        diff = qp.changed(trial.params, report.champion.params)
        assert len(diff) == 1 or trial.name in ("quay lại bộ trước", "về mặc định")
    rows = cal.last_reports()
    assert rows and rows[-1]["as_of"] == data.dates[-1]
    if report.accepted:
        assert qp.active() == report.accepted.params
    else:
        assert qp.active() == qp.DEFAULT


@pytest.mark.unit
def test_an_accepted_change_must_win_on_the_holdout(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_PARAMS_PATH", str(tmp_path / "quant_params.json"))
    data = _dataset()
    scores = iter([])

    def fake_score(ledger):
        return next(scores)

    # champion train, champion holdout, then one train score per challenger; the best wins on
    # train but loses on the holdout, so nothing changes.
    n = len(cal.neighbours(qp.DEFAULT))
    good = sim.Score(10, None, 1, 10, 60, 9.5)
    bad = sim.Score(-5, None, 5, 3, 0, -7.5)
    base = sim.Score(0, None, 0, 8, None, 0)
    scores = iter([base, base] + [good] + [base] * (n - 1) + [bad])
    monkeypatch.setattr(sim, "score", fake_score)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is None and "không giữ được" in report.note
    assert qp.active() == qp.DEFAULT


@pytest.mark.unit
def test_a_quiet_holdout_does_not_block_a_change_that_wins_on_training(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_PARAMS_PATH", str(tmp_path / "quant_params.json"))
    data = _dataset()
    n = len(cal.neighbours(qp.DEFAULT))
    good = sim.Score(10, None, 1, 10, 60, 9.5)
    flat = sim.Score(0, None, 0, 0, None, 0)            # risk-off holdout: nobody traded
    base = sim.Score(0, None, 0, 8, None, 0)
    scores = iter([base, flat] + [good] + [base] * (n - 1) + [flat])
    monkeypatch.setattr(sim, "score", lambda ledger: next(scores))
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is not None
    assert len(qp.changed(qp.active())) == 1


# ─── Momentum rotation ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_momentum_ranks_holds_and_goes_to_cash_under_the_market_average():
    from tradingagents.trading import momentum as mom
    from tradingagents.trading.ledger import Position

    p = replace(qp.DEFAULT, mom_top_k=2, mom_lookback=5, mom_market_ma=10, mom_rebalance=3)
    book = Ledger(cash=1_000_000_000, start_cash=1_000_000_000)
    closes = {"A": [10, 11, 12, 13, 14, 15], "B": [10, 10, 10, 10, 10, 9], "C": [10, 10.5, 11, 11, 11, 11]}
    rising = [100 + i for i in range(12)]
    d = mom.decide(book, closes, rising, p)
    assert d.market_ok and d.rebalanced and d.basket == ["A", "C"]          # B fell: not held
    orders = mom.plan(book, "2026-01-05", d, {s: c[-1] * 1000 for s, c in closes.items()}, p)
    assert {o.symbol for o in orders} == {"A", "C"} and all(o.limit is None and o.qty % 100 == 0 for o in orders)
    assert not mom.decide(book, closes, rising, p).rebalanced                # not yet time to re-rank
    book.positions["A"] = Position("A", 1000, 15_000, "2026-01-06", 0, None, None, sessions=3, last_price=15_000)
    falling = [120 - i for i in range(12)]
    d = mom.decide(book, closes, falling, p)
    assert not d.market_ok and d.basket == []
    sells = mom.plan(book, "2026-01-08", d, {"A": 15_000}, p)
    assert [(o.symbol, o.side) for o in sells if o.side == "SELL"] == [("A", "SELL")]


@pytest.mark.unit
def test_market_orders_fill_at_the_open_and_sells_pay_for_buys_the_same_session():
    from tradingagents.trading.ledger import Order, Position

    book = Ledger(cash=0, start_cash=10_000_000)
    book.positions["OLD"] = Position("OLD", 1000, 10_000, "2026-01-01", 0, None, None, sessions=5, last_price=10_000)
    book.orders = [Order("OLD", "SELL", 1000, None, "2026-01-05"), Order("NEW", "BUY", 500, None, "2026-01-05", ref=19_000)]
    fills = book.process_session({"OLD": Bar("2026-01-06", 10_000, 10_100, 9_900, 10_000, 10_000),
                                  "NEW": Bar("2026-01-06", 19_000, 19_500, 18_800, 19_200, 19_000)})
    assert [f.side for f in fills] == ["SELL", "BUY"] and "NEW" in book.positions and book.cash >= 0


@pytest.mark.unit
def test_momentum_is_calibrated_on_its_own_fields_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_PARAMS_PATH", str(tmp_path / "quant_params.json"))
    data = _dataset(260)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data, strategy="momentum")
    assert report.strategy == "momentum"
    for trial in report.tried:
        assert set(qp.changed(trial.params, report.champion.params)) <= set(qp.MOMENTUM_TUNABLE)
