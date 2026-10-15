"""Regression tests for the quant policy/engine review fixes:

- setup strength counts only in the direction it argues (an oversold bounce
  setup must not push a holder towards SELL),
- resistance targets sit on the tick grid AT OR BELOW the level they aim at,
- suggested shares use a marked NAV and never exceed the free cash.
"""

from dataclasses import replace

import pytest

from tests.test_quant_policy import _closes_downtrend, _make_inputs
from tradingagents.market import vn_rules
from tradingagents.portfolio import PortfolioContext
from tradingagents.quant import engine, features as feat, policy as pol

# ─── Setup strength must follow the bias direction ──────────────────────────

def _oversold_holder():
    inputs = _make_inputs(_closes_downtrend(90), vnindex_bullish=False, relative_strength=-6.0)
    inputs.is_holding = True
    return inputs


def _with_setup(monkeypatch, setup_type, strength):
    monkeypatch.setattr(pol, "_detect_setup", lambda *a, **k: (setup_type, strength, []))


@pytest.mark.unit
def test_oversold_mean_reversion_does_not_boost_a_sell(monkeypatch):
    inputs = _oversold_holder()
    real = pol.evaluate_policy(inputs)
    assert pol._classify_bias(inputs).direction == "bearish"
    assert real.setup_type == "mean_reversion"

    _with_setup(monkeypatch, "none", 0.35)
    baseline = pol.evaluate_policy(_oversold_holder())

    # Was 0.75 -> SELL from the bullish setup alone (0.69 without it).
    assert real.confidence <= baseline.confidence
    assert real.action != "SELL"


@pytest.mark.unit
def test_an_upside_breakout_adds_no_more_sell_confidence_than_a_breakdown(monkeypatch):
    _with_setup(monkeypatch, "breakout", 0.8)
    breakout = pol.evaluate_policy(_oversold_holder())
    _with_setup(monkeypatch, "breakdown", 0.6)
    breakdown = pol.evaluate_policy(_oversold_holder())
    _with_setup(monkeypatch, "none", 0.35)
    none = pol.evaluate_policy(_oversold_holder())

    assert breakout.confidence == none.confidence
    assert breakdown.confidence > breakout.confidence


@pytest.mark.unit
@pytest.mark.parametrize(
    ("direction", "setup", "strength", "expected"),
    [
        ("bearish", "mean_reversion", 0.5, 0.35),
        ("bearish", "breakout", 0.9, 0.35),
        ("bearish", "pullback", 0.65, 0.35),
        ("bearish", "breakdown", 0.6, 0.6),
        ("bearish", "none", 0.3, 0.3),
        ("bullish", "breakdown", 0.4, 0.35),
        ("bullish", "breakout", 0.9, 0.9),
        ("bullish", "mean_reversion", 0.5, 0.5),
        ("conflict", "breakdown", 0.4, 0.35),
    ],
)
def test_directional_setup_strength(direction, setup, strength, expected):
    bias = pol._Bias(direction, 0, 0)
    assert pol._directional_setup_strength(setup, strength, bias) == expected


# ─── T1 target at or below the resistance, on the tick grid ────────────────

def _stats_at(price):
    closes = [50_000 + i * 60 for i in range(84)]
    closes = [c * price / closes[-1] for c in closes]
    return feat.calc_signal_stats(closes, [500_000.0] * len(closes), price)


def _levels(*prices):
    return feat.KeyLevels(
        supports=[], resistances=[feat.PriceLevel(p, 2, "resistance", 0.5) for p in prices]
    )


@pytest.mark.unit
def test_resistance_target_is_floored_to_the_tick():
    stop, target, rr, basis = pol._compute_stop_target(
        55_000.0, None, _stats_at(55_000.0), 0.0, "buy", None, "HOSE", _levels(57_560.0)
    )
    # Nearest-tick rounding gave 57,600 (above the level) and rr 1.21.
    assert "+resistance" in basis
    assert target == 57_500.0
    assert target <= 57_560.0 and target % vn_rules.tick_size(target, "HOSE") == 0
    entry_high = pol.entry_zone(55_000.0, stop, "HOSE")[1]
    assert rr == round((target - entry_high) / (entry_high - stop), 2)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("level", "exchange", "expected"),
    [(12_340.0, "HOSE", 12_300.0), (23_470.0, "HNX", 23_400.0), (49_990.0, "HOSE", 49_950.0),
     (57_500.0, "HOSE", 57_500.0), (9_995.0, "HOSE", 9_990.0)],
)
def test_target_at_or_below(level, exchange, expected):
    assert pol._target_at_or_below(level, exchange) == expected


@pytest.mark.unit
def test_skip_near_resistance_measures_the_floored_target():
    params = replace(pol.DEFAULT_PARAMS, skip_near_resistance=1, min_rr_ratio=1.2)
    _, target, rr, _ = pol._compute_stop_target(
        55_000.0, None, _stats_at(55_000.0), 0.0, "buy", None, "HOSE",
        _levels(57_560.0, 60_040.0), params,
    )
    # 57,560 floors to 57,500 = 1.16R < 1.2: aim at the next level instead.
    assert target == 60_000.0
    assert rr >= 1.2


@pytest.mark.unit
def test_target2_skips_the_level_t1_already_aims_at():
    plan = pol.build_trade_plan(
        55_000.0, 53_400.0, 57_500.0, 0.8, _levels(57_560.0, 60_040.0), None, "HOSE"
    )
    assert plan is not None
    assert plan.target1 == 57_500.0
    assert plan.target2 == 60_000.0


# ─── Suggested shares: marked NAV, capped by free cash ─────────────────────

def _book(cash, *positions):
    return PortfolioContext.model_validate({
        "cash": cash,
        "positions": [{"ticker": t, "quantity": q, "average_price": a} for t, q, a in positions],
    })


@pytest.mark.unit
def test_nav_marks_the_analysed_symbol_at_the_current_close():
    book = _book(10_000_000, ("FPT", 1000, 90_000))
    assert engine._estimated_nav(book, 60_000.0, "FPT") == 70_000_000
    # Another symbol's price says nothing about FPT: cost basis stays.
    assert engine._estimated_nav(book, 60_000.0, "HPG") == 100_000_000


@pytest.mark.unit
def test_nav_counts_the_analysed_position_without_a_cost_basis():
    book = _book(10_000_000, ("FPT", 1000, None))
    assert engine._estimated_nav(book, 60_000.0, "FPT") == 70_000_000


@pytest.mark.unit
def test_suggested_shares_never_exceed_the_free_cash():
    book = _book(10_000_000, ("FPT", 1000, 90_000))
    nav = engine._estimated_nav(book, 60_000.0, "FPT")
    # 20% of 70M at 60k = 200 shares (12M) but only 10M is free -> 100 shares.
    assert vn_rules.shares_for(nav, 20.0, 60_000.0) == 200
    assert engine._suggested_shares(book, nav, 20.0, 60_000.0) == 100


@pytest.mark.unit
def test_suggested_shares_without_a_nav_is_none():
    assert engine._suggested_shares(None, None, 20.0, 60_000.0) is None
    book = _book(500_000_000)
    assert engine._suggested_shares(book, 500_000_000, 10.0, 50_000.0) == 1000
