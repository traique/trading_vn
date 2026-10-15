"""Regression tests for the nightly calibration and the simulator's data (no network).

- a challenger must trade on the holdout; a quiet champion cannot veto its own replacement;
- the best of several challengers must win by a margin that grows with their number;
- a walk-forward check on disjoint windows of the training part;
- at most one change per strategy every ``MIN_SESSIONS_BETWEEN_CHANGES`` sessions;
- history I/O: Vietnam-time stamps, crash-safe appends, malformed lines skipped;
- execution bars from unfilled OHLC, no look-ahead, T+2 honoured.
"""

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.quant import params as qp
from tradingagents.trading import calibrate as cal, simulator as sim
from tradingagents.trading.ledger import Bar

FIELD = "confidence_buy_min"


def _dates(n):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-01", periods=n)]


def _score(value, trades=8, dd=1.0):
    return sim.Score(return_pct=value + 0.5 * dd, benchmark_pct=None, max_drawdown_pct=dd,
                     trades=trades, win_rate_pct=None, value=value)


def _neighbour(direction):
    current = getattr(qp.DEFAULT, FIELD)
    low, high, step = qp.TUNABLE[FIELD]
    return replace(qp.DEFAULT, **{FIELD: cal._step(current, low, high, step, direction)})


def _fake_market(monkeypatch, data, table):
    """``sim.run`` returns (params, start, end) and ``sim.score`` asks ``table(params, part)``,
    part being "train", "holdout" or a walk-forward window index."""
    holdout_from = data.dates[-cal.HOLDOUT_SESSIONS]
    train_end = data.dates[-cal.HOLDOUT_SESSIONS - 1]
    folds = cal._folds([d for d in data.dates if d <= train_end])
    calls = []

    def run(_data, p, *, start=None, end=None, **_):
        return (p, start, end)

    def score(key):
        p, start, end = key
        if start is None and end == train_end:
            part = "train"
        elif start == holdout_from and end is None:
            part = "holdout"
        else:
            part = folds.index((start, end))
        calls.append((p, part))
        return table(p, part)

    monkeypatch.setattr(sim, "run", run)
    monkeypatch.setattr(sim, "score", score)
    return calls


@pytest.fixture
def params_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_PARAMS_PATH", str(tmp_path / "quant_params.json"))
    return tmp_path


def _data(n=120):
    return sim.Dataset(dates=_dates(n), symbols=["AAA"])


# ─── 1. The holdout must hold real evidence ──────────────────────────────────


@pytest.mark.unit
def test_a_challenger_that_stops_trading_does_not_beat_a_losing_champion(params_dir, monkeypatch):
    data = _data()
    winner = _neighbour(+1)

    def table(p, part):
        if p == winner:
            return _score(9.0, trades=10) if part == "train" else _score(0.0, trades=0, dd=0.0)
        return _score(0.0) if part == "train" else _score(-6.0, trades=5, dd=8.0)   # champion in a drawdown

    _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is None
    assert "không giữ được" in report.note
    assert qp.active() == qp.DEFAULT


@pytest.mark.unit
def test_a_quiet_holdout_for_the_champion_is_not_evidence_for_a_change(params_dir, monkeypatch):
    data = _data()
    winner = _neighbour(+1)

    def table(p, part):
        if part == "train":
            return _score(9.0, trades=10) if p == winner else _score(0.0)
        return _score(0.0, trades=0, dd=0.0)                    # risk-off: nobody traded

    _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is None and "chưa đủ bằng chứng" in report.note
    assert qp.active() == qp.DEFAULT


@pytest.mark.unit
def test_a_challenger_with_real_holdout_evidence_is_still_accepted(params_dir, monkeypatch):
    data = _data()
    winner = _neighbour(+1)

    def table(p, part):
        if p == winner:
            return _score(9.0, trades=10) if part == "train" else _score(3.0, trades=4)
        return _score(0.0) if part == "train" else _score(-2.0, trades=5)

    _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is not None and report.accepted.params == winner
    assert qp.active() == winner
    assert cal.last_reports()[-1]["applied"] is True


@pytest.mark.unit
def test_a_quiet_champion_is_replaced_by_a_challenger_that_trades_on_the_holdout(params_dir, monkeypatch):
    # Regression: a champion that stopped trading (0 holdout buys) used to block every
    # change, so a do-nothing set could keep the bot in cash for good.
    data = _data()
    winner = _neighbour(+1)

    def table(p, part):
        if p == winner:
            return _score(9.0, trades=10) if part == "train" else _score(1.0, trades=3)
        return _score(0.0) if part == "train" else _score(0.0, trades=0, dd=0.0)

    _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is not None and report.accepted.params == winner
    assert qp.active() == winner


@pytest.mark.unit
def test_a_quiet_champion_still_needs_the_trading_challenger_to_be_no_worse(params_dir, monkeypatch):
    data = _data()
    winner = _neighbour(+1)

    def table(p, part):
        if p == winner:
            return _score(9.0, trades=10) if part == "train" else _score(-3.0, trades=3)
        return _score(0.0) if part == "train" else _score(0.0, trades=1, dd=0.0)

    _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is None and "chưa đủ bằng chứng" in report.note
    assert qp.active() == qp.DEFAULT


@pytest.mark.unit
def test_a_quiet_drifted_champion_can_go_back_to_the_defaults(params_dir, monkeypatch):
    # Two fields drifted, so no one-step neighbour equals the defaults.
    low, high, step = qp.TUNABLE["min_rr_ratio"]
    strict = replace(_neighbour(+1), min_rr_ratio=cal._step(qp.DEFAULT.min_rr_ratio, low, high, step, +1))
    qp.save(strict, note="drifted")

    def table(p, part):
        if p == qp.DEFAULT:
            return _score(9.0, trades=10) if part == "train" else _score(0.0, trades=0, dd=0.0)
        return _score(0.0) if part == "train" else _score(0.0, trades=0, dd=0.0)

    data = _data()
    _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is not None and report.accepted.name == cal.RESET
    assert qp.active() == qp.DEFAULT


# ─── 2b. Several challengers: the best must win by a larger margin ───────────


def _three_promising(monkeypatch, data, holdout_values):
    trained = [_neighbour(-1), _neighbour(+1), replace(qp.DEFAULT, min_rr_ratio=qp.DEFAULT.min_rr_ratio + 0.1)]
    train_values = [12.0, 11.0, 10.0]

    def table(p, part):
        if p in trained:
            i = trained.index(p)
            return _score(train_values[i], trades=10) if part == "train" else _score(holdout_values[i], trades=4)
        return _score(0.0) if part == "train" else _score(0.0, trades=4)

    _fake_market(monkeypatch, data, table)
    return trained


@pytest.mark.unit
def test_the_holdout_margin_grows_with_the_number_of_challengers(params_dir, monkeypatch):
    data = _data()
    # Each beats the champion by 0.5 on the holdout: enough for one challenger, not for three.
    _three_promising(monkeypatch, data, [0.5, 0.5, 0.5])
    assert cal.holdout_margin(1) < 0.5 < cal.holdout_margin(3)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is None
    assert qp.active() == qp.DEFAULT


@pytest.mark.unit
def test_the_best_on_the_holdout_wins_not_the_first_that_passes(params_dir, monkeypatch):
    data = _data()
    trained = _three_promising(monkeypatch, data, [1.0, 2.5, 1.5])
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is not None and report.accepted.params == trained[1]


# ─── 2c. Walk-forward on the training part ───────────────────────────────────


@pytest.mark.unit
def test_a_gain_from_one_lucky_stretch_of_training_is_rejected(params_dir, monkeypatch):
    data = _data(cal.HOLDOUT_SESSIONS + cal.WALK_FORWARD_FOLDS * cal.MIN_FOLD_SESSIONS + 3)
    winner = _neighbour(+1)

    def table(p, part):
        if p == winner:
            return {"train": _score(9.0, trades=10), "holdout": _score(3.0, trades=4),
                    0: _score(-1.0), 1: _score(20.0), 2: _score(0.5)}[part]
        return _score(0.0) if part != "holdout" else _score(0.0, trades=4)

    calls = _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is None and "giai đoạn" in report.note
    assert (winner, "holdout") not in calls               # it never reached the holdout
    assert report.champion.folds and len(report.champion.folds) == cal.WALK_FORWARD_FOLDS


@pytest.mark.unit
def test_a_challenger_steady_on_every_window_goes_through(params_dir, monkeypatch):
    data = _data(cal.HOLDOUT_SESSIONS + cal.WALK_FORWARD_FOLDS * cal.MIN_FOLD_SESSIONS + 3)
    winner = _neighbour(+1)

    def table(p, part):
        if p == winner:
            return {"train": _score(9.0, trades=10), "holdout": _score(3.0, trades=4)}.get(part, _score(1.0))
        return _score(0.0) if part != "holdout" else _score(0.0, trades=4)

    _fake_market(monkeypatch, data, table)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is not None and report.accepted.params == winner
    row = cal.last_reports()[-1]
    assert len(row["accepted"]["folds"]) == cal.WALK_FORWARD_FOLDS


@pytest.mark.unit
def test_folds_are_disjoint_consecutive_and_skipped_when_too_short():
    train = _dates(cal.WALK_FORWARD_FOLDS * cal.MIN_FOLD_SESSIONS + 2)
    folds = cal._folds(train)
    assert len(folds) == cal.WALK_FORWARD_FOLDS
    assert folds[0][0] == train[0] and folds[-1][1] == train[-1]
    for (_, end), (start, _) in zip(folds, folds[1:], strict=False):
        assert train.index(start) == train.index(end) + 1
    assert cal._folds(train[: cal.WALK_FORWARD_FOLDS * cal.MIN_FOLD_SESSIONS - 1]) == []


# ─── 2a. Rate limit ──────────────────────────────────────────────────────────


def _always_winner(monkeypatch, data):
    winner = _neighbour(+1)

    def table(p, part):
        if p == winner:
            return _score(9.0, trades=10) if part == "train" else _score(3.0, trades=4)
        return _score(0.0) if part == "train" else _score(0.0, trades=4)

    return winner, _fake_market(monkeypatch, data, table)


def _write_history(rows, raw_lines=()):
    path = cal.history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows] + list(raw_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _change_row(as_of, strategy="engine", applied=True, champion=qp.DEFAULT):
    trial = {"name": "x", "params": qp.to_dict(champion)}
    return {"as_of": as_of, "strategy": strategy, "champion": trial, "accepted": trial, "applied": applied}


@pytest.mark.unit
def test_no_second_change_within_the_rate_limit(params_dir, monkeypatch):
    data = _data()
    _write_history([_change_row(data.dates[-5])])
    _, calls = _always_winner(monkeypatch, data)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is None and report.tried == []
    assert "4 phiên" in report.note
    assert {part for _, part in calls} == {"train", "holdout"} and len(calls) == 2   # champion only
    assert qp.active() == qp.DEFAULT


@pytest.mark.unit
def test_the_rate_limit_ends_after_enough_sessions_and_ignores_other_strategies_and_dry_runs(
        params_dir, monkeypatch):
    data = _data()
    gap = cal.MIN_SESSIONS_BETWEEN_CHANGES
    _write_history([_change_row(data.dates[-1 - gap]),
                    _change_row(data.dates[-2], strategy="momentum"),
                    _change_row(data.dates[-2], applied=False)])
    winner, _ = _always_winner(monkeypatch, data)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert report.accepted is not None and report.accepted.params == winner


@pytest.mark.unit
def test_a_dry_run_is_recorded_as_not_applied_and_does_not_start_the_rate_limit(params_dir, monkeypatch):
    data = _data()
    _always_winner(monkeypatch, data)
    first = cal.calibrate(["AAA"], data.dates[-1], data=data, apply=False)
    assert first.accepted is not None and qp.active() == qp.DEFAULT
    assert cal.last_reports()[-1]["applied"] is False
    second = cal.calibrate(["AAA"], data.dates[-1], data=data)
    assert second.accepted is not None


@pytest.mark.unit
def test_a_same_night_rerun_after_a_change_changes_nothing(params_dir, monkeypatch):
    data = _data()
    _always_winner(monkeypatch, data)
    assert cal.calibrate(["AAA"], data.dates[-1], data=data).accepted is not None
    again = cal.calibrate(["AAA"], data.dates[-1], data=data, champion=qp.active())
    assert again.accepted is None and "0 phiên" in again.note


# ─── 4. History I/O ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_history_is_stamped_in_vietnam_time(params_dir, monkeypatch):
    data = _data()
    _always_winner(monkeypatch, data)
    cal.calibrate(["AAA"], data.dates[-1], data=data, apply=False)
    at = cal.last_reports()[-1]["at"]
    assert at.endswith("+07:00")


@pytest.mark.unit
def test_malformed_history_lines_are_skipped_not_fatal(params_dir, monkeypatch):
    data = _data()
    old = replace(qp.DEFAULT, **{FIELD: _neighbour(-1).confidence_buy_min})
    current = _neighbour(+1)
    bad_params = {"as_of": data.dates[0], "strategy": "engine", "applied": True,
                  "champion": {"params": "not a dict"}, "accepted": {"name": "y"}}
    _write_history([_change_row(data.dates[-40], champion=old), bad_params],
                   raw_lines=["{not json", "[1, 2, 3]", "42", '{"as_of": "2026-0'])
    assert cal._previous_champion(current) == old
    assert all(isinstance(r, dict) for r in cal.last_reports(50))
    _always_winner(monkeypatch, data)
    report = cal.calibrate(["AAA"], data.dates[-1], data=data, champion=current)
    assert any(t.name == "quay lại bộ trước" for t in report.tried)


@pytest.mark.unit
def test_an_append_after_a_line_cut_by_a_crash_starts_on_its_own_line(params_dir, monkeypatch):
    data = _data()
    path = cal.history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"as_of": "2026-01-01", "note": "ok"}\n{"as_of": "2026-01-02", "no', encoding="utf-8")
    _always_winner(monkeypatch, data)
    cal.calibrate(["AAA"], data.dates[-1], data=data, apply=False)
    rows = cal.last_reports(10)
    assert [r["as_of"] for r in rows] == ["2026-01-01", data.dates[-1]]
    assert path.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.unit
def test_missing_or_unreadable_history_is_empty(params_dir):
    assert cal.last_reports() == [] and cal._previous_champion(qp.DEFAULT) is None
    cal.history_path().mkdir(parents=True)           # a directory where the file should be
    assert cal.last_reports() == []


# ─── 3. Simulator data: unfilled execution bars, no look-ahead, T+2 ──────────


def _frame(n=90, gap_at=70):
    dates = pd.bdate_range("2026-01-01", periods=n)
    close = [20_000 + 50 * i for i in range(n)]
    frame = pd.DataFrame({"Date": dates, "Open": [c - 50 for c in close], "High": [c + 150 for c in close],
                          "Low": [c - 200 for c in close], "Close": close, "Volume": [1e6] * n})
    if gap_at is not None:
        frame.loc[gap_at, ["Open", "High", "Low", "Volume"]] = float("nan")
    return frame


@pytest.mark.unit
def test_execution_bars_are_unfilled_while_indicators_get_filled_prices(monkeypatch):
    from tradingagents.market import vn_rules

    stock, vni = _frame(), _frame(gap_at=None)
    asked, seen = [], []

    def fake_load(symbol, as_of, fill_gaps=True, sources=None):
        asked.append((symbol, fill_gaps))
        return (vni if symbol == vn_rules.BENCHMARK else stock).copy()

    def fake_inputs(symbol, key, frame, *a, **k):
        seen.append(frame)
        return SimpleNamespace(inputs=key)

    monkeypatch.setattr(sim, "load_ohlcv", fake_load)
    monkeypatch.setattr(sim, "exchange_of", lambda s: "HOSE")
    monkeypatch.setattr(sim.engine, "build_inputs", fake_inputs)
    data = sim.build_dataset(["AAA"], 30, "2026-12-31", flows=False)

    assert ("AAA", False) in asked
    gap_day = stock["Date"].iloc[70].strftime("%Y-%m-%d")
    bar = data.bars["AAA"][gap_day]
    assert math.isnan(bar.open) and math.isnan(bar.high) and math.isnan(bar.low)
    assert bar.close == stock["Close"].iloc[70] and bar.prev_close == stock["Close"].iloc[69]
    assert bar.exchange == "HOSE"
    assert not bar.tradable                                 # the ledger trades nothing on it
    before = data.bars["AAA"][stock["Date"].iloc[69].strftime("%Y-%m-%d")]
    assert before.tradable and before.volume == 1e6
    # A missing volume alone is unknown, not "no trading".
    assert bar.volume is None
    # The decision on the gap day is still made, on gap-filled indicator inputs.
    assert ("AAA", gap_day) in data.inputs
    assert all(not f[["Open", "High", "Low", "Close"]].isna().any().any() for f in seen)
    # Each decision reads bars up to its own day only.
    assert all(pd.Timestamp(f["Date"].iloc[-1]).strftime("%Y-%m-%d") == k
               for f, k in zip(seen, [k for (_, k) in data.inputs], strict=True))


def _momentum_dataset(n=200):
    dates = _dates(n)
    data = sim.Dataset(dates=dates, symbols=["A", "B", "C"])
    for j, symbol in enumerate(data.symbols):
        bars, prev = {}, None
        for i, day in enumerate(dates):
            # Leaders change every ~40 sessions, so the basket rotates and positions are sold.
            phase = (i // 40 + j) % 3
            close = round(10_000 * (1 + 0.004 * i * (1 if phase == 0 else -0.5 if phase == 1 else 0.2)), -1)
            bars[day] = Bar(day, close, close * 1.02, close * 0.98, close, prev, exchange="HOSE")
            prev = close
        data.bars[symbol] = bars
    data.benchmark = {d: 1000 + i for i, d in enumerate(dates)}
    return data


def _cut(data, last):
    keep = [d for d in data.dates if d <= last]
    return sim.Dataset(
        dates=keep, symbols=list(data.symbols),
        inputs={k: v for k, v in data.inputs.items() if k[1] <= last},
        bars={s: {d: b for d, b in bars.items() if d <= last} for s, bars in data.bars.items()},
        benchmark={d: v for d, v in data.benchmark.items() if d <= last})


@pytest.mark.unit
def test_momentum_has_no_look_ahead_and_honours_t_plus_2():
    from tradingagents.trading import momentum

    p = replace(qp.DEFAULT, mom_top_k=1, mom_lookback=42, mom_market_ma=50, mom_rebalance=10)
    data = _momentum_dataset()
    full = momentum.run(data, p)
    assert any(f.side == "SELL" for f in full.fills), "the synthetic market must rotate the basket"
    cut_day = data.dates[150]
    part = momentum.run(_cut(data, cut_day), p)
    # What happened up to D does not depend on anything after D.
    assert [h for h in full.history if h["date"] <= cut_day] == part.history
    assert [f for f in full.fills if f.date <= cut_day] == part.fills
    index = {d: i for i, d in enumerate(data.dates)}
    last_buy = {}
    for f in full.fills:
        if f.side == "BUY":
            last_buy[f.symbol] = index[f.date]
        else:
            assert index[f.date] - last_buy[f.symbol] >= 2       # T+2 at the earliest


@pytest.mark.unit
def test_the_engine_simulation_has_no_look_ahead():
    from tests.test_trading import _dataset

    data = _dataset(160)
    full = sim.run(data, qp.DEFAULT)
    cut_day = data.dates[100]
    part = sim.run(_cut(data, cut_day), qp.DEFAULT)
    assert [h for h in full.history if h["date"] <= cut_day] == part.history
    assert [f for f in full.fills if f.date <= cut_day] == part.fills
