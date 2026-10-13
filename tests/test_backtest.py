"""Backtesting: many single-shot decisions, scored by the memory log.

A run already records its rating and later settles it with realized and alpha
return against VNINDEX. A backtest is that machinery over a grid of tickers and
dates, aggregated. It evaluates decision quality; it does not simulate a
portfolio, so there is no execution and no equity curve; a BUY's alpha is also
reported net of the Vietnamese round-trip costs.
"""

from __future__ import annotations

import pytest

from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.memory import TradingMemoryLog

DECISION = "Rating: Buy\n\nbuy it"


@pytest.mark.unit
def test_grid_spacing_and_canonical_dates():
    assert iter_grid("2026-01-05", "2026-01-20", every_n_days=7) == ["2026-01-05", "2026-01-12", "2026-01-19"]


@pytest.mark.unit
def test_grid_stops_at_today(monkeypatch):
    import tradingagents.backtest as bt

    monkeypatch.setattr(bt, "get_current_date", lambda: "2026-01-10")
    assert iter_grid("2026-01-05", "2026-02-20", every_n_days=5) == ["2026-01-05", "2026-01-10"]


@pytest.mark.unit
def test_grid_rejects_a_non_canonical_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        iter_grid("2026-1-5", "2026-01-20")


class _FakeGraph:
    """Stands in for TradingAgentsGraph, writing to the log the harness gave it."""

    instances: list = []
    fail_on: set = set()

    def __init__(self, selected_analysts=None, config=None, **kw):
        self.analysts = list(selected_analysts) if selected_analysts else None
        self.config = config
        self.memory_log = TradingMemoryLog(config)
        self.calls = []
        self.settled = []
        _FakeGraph.instances.append(self)

    def propagate(self, ticker, trade_date, asset_type="stock", portfolio=None):
        self.calls.append((ticker, trade_date))
        if (ticker, trade_date) in _FakeGraph.fail_on:
            raise RuntimeError("vendor exploded")
        self.memory_log.store_decision(ticker, trade_date, DECISION)
        return {"final_trade_decision": DECISION}, "BUY"

    def settle_pending(self, ticker):
        self.settled.append(ticker)


@pytest.fixture(autouse=True)
def _fake_graph(monkeypatch, tmp_path):
    import tradingagents.backtest as bt

    _FakeGraph.instances = []
    _FakeGraph.fail_on = set()
    monkeypatch.setattr(bt, "TradingAgentsGraph", _FakeGraph)
    return _FakeGraph


def _config(tmp_path):
    return {"results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "live_trading_memory.md")}


@pytest.mark.unit
def test_the_live_memory_log_is_never_written(tmp_path):
    config = _config(tmp_path)
    result = run_backtest(["FPT"], ["2026-01-05", "2026-01-12"], config)

    assert not (tmp_path / "live_trading_memory.md").exists()
    assert result.log_path.exists() and result.cells_run == 2


@pytest.mark.unit
def test_a_cell_already_in_the_log_is_not_run_again(tmp_path):
    config = _config(tmp_path)
    first = run_backtest(["FPT"], ["2026-01-05"], config)

    again = run_backtest(["FPT"], ["2026-01-05", "2026-01-12"], config, run_id=first.run_id)

    assert again.cells_run == 1 and again.skipped == 1
    assert _FakeGraph.instances[-1].calls == [("FPT", "2026-01-12")]


@pytest.mark.unit
def test_every_ticker_is_settled_after_the_grid(tmp_path):
    """Settlement runs at the start of the next same-ticker run, so the last
    date of each ticker would stay pending without an explicit pass."""
    run_backtest(["FPT", "HPG"], ["2026-01-05", "2026-01-12"], _config(tmp_path))
    assert sorted(_FakeGraph.instances[-1].settled) == ["FPT", "HPG"]


@pytest.mark.unit
def test_a_failed_cell_does_not_abort_the_sweep(tmp_path):
    _FakeGraph.fail_on = {("FPT", "2026-01-05")}
    result = run_backtest(["FPT"], ["2026-01-05", "2026-01-12"], _config(tmp_path))

    assert result.cells_run == 1
    assert result.failures == [("FPT", "2026-01-05", "vendor exploded")]


# --- reading the result ------------------------------------------------------

def _log_with(tmp_path, rows):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    for ticker, date, decision, outcome in rows:
        log.store_decision(ticker, date, decision)
        if outcome is not None:
            log.update_with_outcome(ticker, date, outcome[0], outcome[1], 5, "note", "2026-02-01")
    return tmp_path / "m.md"


@pytest.mark.unit
def test_summary_scores_resolved_cells_and_keeps_pending_out_of_the_average(tmp_path):
    log = _log_with(tmp_path, [
        ("FPT", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04)),
        ("FPT", "2026-01-12", "Rating: Buy\n\nx", (-0.02, -0.02)),
        ("HPG", "2026-01-05", "Rating: Sell\n\nx", None),
    ])

    summary = summarize(log)

    assert summary.resolved == 2 and summary.pending == 1
    buys = summary.by_rating["BUY"]
    assert buys.count == 2 and buys.hit_rate == 0.5 and round(buys.mean_alpha, 4) == 0.01
    assert "SELL" not in summary.by_rating  # unsettled: nothing to score yet


@pytest.mark.unit
def test_a_buy_is_also_scored_after_round_trip_costs(tmp_path):
    """A BUY earns its alpha minus fees, sales tax and slippage; the summary
    reports that net figure next to the gross one."""
    from tradingagents.market import vn_rules

    summary = summarize(_log_with(tmp_path, [
        ("FPT", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04)),
        ("HPG", "2026-01-05", "Rating: Sell\n\nx", (-0.05, -0.03)),
    ]))
    cost = vn_rules.round_trip_cost_pct() / 100
    assert cost > 0
    assert summary.by_rating["BUY"].mean_alpha_net == pytest.approx(0.04 - cost)
    assert summary.by_rating["SELL"].mean_alpha_net is None  # an exit has no round trip to charge
    text = summary.render()
    assert f"{0.04 - cost:+.2%} after costs" in text
    assert text.count("after costs") == 1


@pytest.mark.unit
def test_tickers_are_normalised_to_the_code_the_log_records(tmp_path):
    """"fpt" and "FPT.VN" are the same cell as "FPT"."""
    run_backtest(["fpt", "FPT.VN", "FPT"], ["2026-01-05"], _config(tmp_path))
    graph = _FakeGraph.instances[-1]
    assert graph.calls == [("FPT", "2026-01-05")]
    assert graph.settled == ["FPT"]


@pytest.mark.unit
def test_summary_states_what_it_cannot_prove(tmp_path):
    text = summarize(_log_with(tmp_path, [("FPT", "2026-01-05", DECISION, (0.1, 0.05))])).render()
    assert "not archived" in text
    assert "one" in text.lower() and "sampl" in text.lower()


@pytest.mark.unit
def test_the_analyst_set_under_test_is_the_one_that_runs(tmp_path):
    """A backtest of a two-analyst setup must not silently run four."""
    run_backtest(["FPT"], ["2026-01-05"], _config(tmp_path), selected_analysts=["market", "news"])
    assert _FakeGraph.instances[-1].analysts == ["market", "news"]


@pytest.mark.unit
def test_a_run_id_cannot_escape_the_results_directory(tmp_path):
    """run_id becomes a path segment, so it is validated like a ticker is."""
    with pytest.raises(ValueError):
        run_backtest(["FPT"], ["2026-01-05"], _config(tmp_path), run_id="../../escaped")
    with pytest.raises(ValueError):
        run_backtest(["FPT"], ["2026-01-05"], _config(tmp_path), run_id="/etc/cron.d/x")


@pytest.mark.unit
def test_a_failed_settlement_does_not_lose_the_remaining_tickers(tmp_path, monkeypatch):
    """Settlement reflects with an LLM, so it can fail; the sweep still returns
    its result and every other ticker still gets settled."""
    settled = []

    def _settle(self, ticker):
        if ticker == "FPT":
            raise RuntimeError("reflector timed out")
        settled.append(ticker)

    monkeypatch.setattr(_FakeGraph, "settle_pending", _settle, raising=False)
    result = run_backtest(["FPT", "HPG"], ["2026-01-05"], _config(tmp_path))

    assert result.cells_run == 2
    assert settled == ["HPG"]
    assert result.settlement_failures == [("FPT", "reflector timed out")]


@pytest.mark.unit
def test_pending_note_appears_only_when_something_is_pending(tmp_path):
    settled = [("FPT", "2026-01-05", DECISION, (0.1, 0.05))]
    assert "Pending" not in summarize(_log_with(tmp_path, settled)).render()
    assert "Pending" in summarize(_log_with(tmp_path, settled + [("HPG", "2026-01-05", DECISION, None)])).render()


# --- scoring reads the direction the rating claimed ---------------------------

def _scored(tmp_path, rows):
    log = _log_with(tmp_path, rows)
    return summarize(log).by_rating


@pytest.mark.unit
def test_a_bearish_call_that_fell_counts_as_right(tmp_path):
    """Alpha below the benchmark is the outcome a Sell predicted; scoring it as
    a miss reported the system as wrong exactly when it was right."""
    scores = _scored(tmp_path, [
        ("FPT", "2026-01-05", "**Rating**: Sell\n\nx", (-0.08, -0.05)),
        ("HPG", "2026-01-05", "**Rating**: Bán\n\nx", (-0.03, -0.02)),
    ])
    assert scores["SELL"].count == 2
    assert scores["SELL"].hit_rate == 1.0


@pytest.mark.unit
def test_a_bearish_call_that_rose_counts_as_wrong(tmp_path):
    scores = _scored(tmp_path, [("FPT", "2026-01-05", "**Rating**: Sell\n\nx", (0.08, 0.05))])
    assert scores["SELL"].hit_rate == 0.0


@pytest.mark.unit
def test_a_bullish_call_is_scored_the_same_way_as_before(tmp_path):
    scores = _scored(tmp_path, [
        ("FPT", "2026-01-05", "**Rating**: Buy\n\nx", (0.10, 0.04)),
        ("HPG", "2026-01-05", "**Rating**: Buy\n\nx", (-0.02, -0.02)),
    ])
    assert scores["BUY"].hit_rate == 0.5


@pytest.mark.unit
def test_hold_claims_no_direction_so_it_gets_no_hit_rate(tmp_path):
    scores = _scored(tmp_path, [("FPT", "2026-01-05", "**Rating**: Hold\n\nx", (0.01, 0.005))])
    assert scores["HOLD"].hit_rate is None
    assert scores["HOLD"].mean_alpha == 0.005
    assert scores["HOLD"].mean_alpha_net is None


@pytest.mark.unit
@pytest.mark.parametrize("action", ["WATCH", "NO_TRADE"])
def test_watch_and_no_trade_claim_no_direction_either(tmp_path, action):
    scores = _scored(tmp_path, [("FPT", "2026-01-05", f"**Rating**: {action}\n\nx", (-0.04, -0.03))])
    assert scores[action].hit_rate is None
    assert scores[action].mean_alpha == -0.03


@pytest.mark.unit
def test_the_report_names_the_window_the_scores_cover(tmp_path):
    text = summarize(_log_with(tmp_path, [
        ("FPT", "2026-01-05", "**Rating**: Buy\n\nx", (0.1, 0.05))])).render()
    assert "5" in text and "day" in text.lower()
    assert "HOLD" not in text or "no direction" in text.lower()


@pytest.mark.unit
def test_the_window_reported_is_the_one_the_outcomes_used(tmp_path):
    """The log records the window each outcome was measured over; the summary
    must not claim a different one."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    log.store_decision("FPT", "2026-01-05", "**Rating**: Buy\n\nx")
    log.update_with_outcome("FPT", "2026-01-05", 0.1, 0.04, 21, "note", "2026-02-01")

    assert "21 trading days" in summarize(tmp_path / "m.md").render()


@pytest.mark.unit
def test_a_backtest_result_is_summarized_directly(tmp_path):
    """The result names its own log, so a caller never builds the log to score it."""
    from tradingagents.backtest import BacktestResult

    path = _log_with(tmp_path, [("FPT", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04))])

    assert summarize(BacktestResult(run_id="r", log_path=path)).resolved == 1


@pytest.mark.unit
def test_a_log_path_that_does_not_exist_is_an_error_not_an_empty_summary(tmp_path):
    missing = tmp_path / "no-such-dir" / "m.md"

    with pytest.raises(FileNotFoundError):
        summarize(missing)
    assert not missing.parent.exists()


@pytest.mark.unit
def test_a_result_whose_cells_all_failed_summarizes_as_empty(tmp_path):
    from tradingagents.backtest import BacktestResult

    result = BacktestResult(run_id="r", log_path=tmp_path / "never-written.md")

    assert summarize(result).resolved == 0


@pytest.mark.unit
def test_progress_is_reported_before_each_cell(tmp_path):
    seen = []

    run_backtest(["FPT", "HPG"], ["2026-01-05", "2026-01-12"], _config(tmp_path),
                 progress=lambda done, total, ticker, date: seen.append((done, total, ticker, date)))

    assert seen == [(1, 4, "FPT", "2026-01-05"), (2, 4, "FPT", "2026-01-12"),
                    (3, 4, "HPG", "2026-01-05"), (4, 4, "HPG", "2026-01-12")]


@pytest.mark.unit
def test_a_resumed_sweep_reports_only_the_cells_it_runs(tmp_path):
    first = run_backtest(["FPT"], ["2026-01-05"], _config(tmp_path))
    seen = []

    run_backtest(["FPT"], ["2026-01-05", "2026-01-12"], _config(tmp_path), run_id=first.run_id,
                 progress=lambda done, total, ticker, date: seen.append((done, total, date)))

    assert seen == [(1, 1, "2026-01-12")]


@pytest.mark.unit
def test_a_ticker_or_date_given_twice_runs_and_settles_once(tmp_path):
    run_backtest(["FPT", "FPT"], ["2026-01-05", "2026-01-05"], _config(tmp_path))
    graph = _FakeGraph.instances[-1]
    assert graph.calls == [("FPT", "2026-01-05")]
    assert graph.settled == ["FPT"]


@pytest.mark.unit
@pytest.mark.parametrize("asset_type", ["crypto", "etf"])
def test_the_graph_runs_only_vietnamese_stocks(asset_type):
    """A sweep cell for another asset type fails loudly instead of being
    analysed as if it were a Vietnamese stock."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)  # no LLM clients needed
    with pytest.raises(ValueError, match="stock"):
        graph.propagate("FPT", "2026-01-05", asset_type)
