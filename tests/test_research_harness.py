"""The research harness (research/): ledger-driven backtests, point in time, TEST locked.

The default suite runs on small synthetic panels (no network, no data).
Tests on the downloaded data are marked ``integration`` and skipped when the
data directory (research/data or TRADING_VN_RESEARCH_DATA) is missing:

    python -m pytest tests/test_research_harness.py -m integration
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from research import config, data as rdata
from research.backtest import Context, TestPeriodLocked, run, run_index
from research.data import Panel
from tradingagents.market import vn_rules

FEE = vn_rules.BUY_FEE_PCT / 100
SELL_COST = (vn_rules.SELL_FEE_PCT + vn_rules.SELL_TAX_PCT) / 100


def make_panel(prices: dict[str, list[float]], start="2019-01-02") -> Panel:
    """A synthetic panel: open = close = the given price, high/low +-1%, volume 1e6, HOSE."""
    n = len(next(iter(prices.values())))
    dates = pd.bdate_range(start, periods=n)
    close = pd.DataFrame(prices, index=dates, dtype=float)
    vol = pd.DataFrame(1e6, index=dates, columns=close.columns)
    vni = pd.Series(np.linspace(1000, 1100, n), index=dates)
    return Panel(
        dates=dates, open=close.copy(), high=close * 1.01, low=close * 0.99, close=close, volume=vol,
        raw_close=close.copy(), value=close * vol, exchange=pd.DataFrame("HOSE", index=dates, columns=close.columns),
        vnindex=vni, vn30=vni.copy(), vnindex_bars=pd.DataFrame({"Close": vni}), vn30_today=list(close.columns),
    )


class Scripted:
    """Orders by session number: {k: callable(ctx) -> orders}."""

    def __init__(self, script):
        self.script, self.k = script, -1

    def on_session(self, ctx):
        self.k += 1
        fn = self.script.get(self.k)
        return fn(ctx) if fn else None


# ── the ledger through the harness ───────────────────────────────────────────


@pytest.mark.unit
def test_buy_and_hold_matches_price_return_minus_costs():
    path = [10_000 + 37 * i for i in range(60)]          # steady rise, 10,000 -> 12,183
    panel = make_panel({"AAA": path})
    cap = 1e9
    res = run(Scripted({0: lambda ctx: [ctx.buy("AAA", weight=0.9)],
                        58: lambda ctx: [ctx.sell("AAA")]}),
              "2019-01-01", "2019-12-31", capital=cap, panel=panel, universe_kind="vn30_today")
    buy, sell = res.fills.iloc[0], res.fills.iloc[1]
    # Buy at session 1's open + slippage on the HOSE tick grid; sell at session 59's open - slippage.
    assert buy.price == vn_rules.ceil_to_tick(path[1] * (1 + vn_rules.SLIPPAGE_PCT / 100), "HOSE")
    assert sell.price == vn_rules.floor_to_tick(path[59] * (1 - vn_rules.SLIPPAGE_PCT / 100), "HOSE")
    qty = buy.qty
    assert qty == vn_rules.round_lot(0.9 * cap / (path[0] * (1 + FEE) * 1.02))
    expected_cash = cap - qty * buy.price * (1 + FEE) + qty * sell.price * (1 - SELL_COST)
    assert res.daily["nav"].iloc[-1] == pytest.approx(expected_cash, abs=2)
    # Net return on the invested money = price return minus fees, tax and 2x slippage.
    invested = qty * buy.price * (1 + FEE)
    net = (qty * sell.price * (1 - SELL_COST)) / invested - 1
    gross = path[59] / path[1] - 1
    cost = 1 - (1 + net) / (1 + gross)
    expected = 1 - (sell.price / path[59] * (1 - SELL_COST)) / (buy.price / path[1] * (1 + FEE))
    assert cost == pytest.approx(expected, abs=1e-9)
    # 0.15% + 0.25% + 2 x 0.1% slippage = 0.6%, plus tick rounding against the trader (here 50đ ticks).
    assert 0.0059 < cost < 0.0090


@pytest.mark.unit
def test_t_plus_2_blocks_sale_on_t_plus_1():
    panel = make_panel({"AAA": [10_000.0] * 10})
    # Buy decided on session 0 -> fills session 1 (T). Ask to sell every session from T's close on.
    res = run(Scripted({0: lambda ctx: [ctx.buy("AAA", weight=0.5)],
                        1: lambda ctx: [ctx.sell("AAA")], 2: lambda ctx: [ctx.sell("AAA")],
                        3: lambda ctx: [ctx.sell("AAA")]}),
              "2019-01-01", "2019-12-31", panel=panel, universe_kind="vn30_today")
    days = list(panel.dates.strftime("%Y-%m-%d"))
    buys = res.fills[res.fills.side == "BUY"]
    sells = res.fills[res.fills.side == "SELL"]
    assert list(buys.date) == [days[1]]
    # No sale on T+1 (days[2]); the sale happens on T+2 (days[3]) at the close (afternoon arrival).
    assert days[2] not in set(sells.date)
    assert list(sells.date) == [days[3]]
    assert sells.iloc[0].price == vn_rules.floor_to_tick(10_000 * (1 - vn_rules.SLIPPAGE_PCT / 100), "HOSE")


@pytest.mark.unit
def test_context_has_no_future_rows():
    panel = make_panel({"AAA": [10_000.0 + i for i in range(30)]})
    seen = []

    def strat(ctx: Context):
        seen.append((ctx.date, ctx.close.index[-1], len(ctx.vnindex), ctx.i, ctx.history("value").index[-1]))

    run(strat, "2019-01-01", "2019-12-31", panel=panel, universe_kind="vn30_today")
    assert len(seen) == 30
    assert all(d == last == vlast and n == i + 1 for d, last, n, i, vlast in seen)


@pytest.mark.unit
def test_test_period_is_locked_in_run():
    panel = make_panel({"AAA": [10_000.0] * 10})
    with pytest.raises(TestPeriodLocked):
        run(lambda ctx: None, "2023-06-01", "2024-03-01", panel=panel)
    with pytest.raises(TestPeriodLocked):
        run_index("2023-06-01", "2024-03-01", panel=panel)
    # A panel that holds TEST sessions is refused without the unlock, whatever the end.
    late = make_panel({"AAA": [10_000.0] * 10}, start="2023-12-25")
    with pytest.raises(TestPeriodLocked):
        run(lambda ctx: None, "2023-12-25", "2023-12-29", panel=late)
    assert run(lambda ctx: None, "2023-12-25", "2024-01-05", panel=late, unlock_test=True).end == "2024-01-05"


@pytest.mark.unit
def test_test_period_is_locked_in_load_panel(monkeypatch):
    full = make_panel({"AAA": [10_000.0] * 40}, start="2023-12-01")
    full.price_history = {"AAA": pd.DataFrame({"MatchValue": 1.0, "RawClose": 1.0, "Shares": 1.0}, index=full.dates)}
    monkeypatch.setattr(rdata, "_FULL", full)
    monkeypatch.setattr(rdata, "_LOCKED", None)
    locked = rdata.load_panel()
    assert locked.dates[-1] <= pd.Timestamp(config.LOCK_AFTER) < full.dates[-1]
    assert locked.close.index[-1] <= pd.Timestamp(config.LOCK_AFTER)
    assert locked.price_history["AAA"].index[-1] <= pd.Timestamp(config.LOCK_AFTER)
    assert rdata.load_panel(unlock_test=True) is full


@pytest.mark.unit
def test_the_command_line_refuses_test_without_the_unlock(capsys):
    from research.cli import main

    assert main(["run", "factor", "--split", "TEST"]) == 2
    assert main(["run", "momentum", "--split", "ALL"]) == 2
    assert main(["run", "ew", "--start", "2023-01-01", "--end", "2024-02-01"]) == 2
    assert "TEST is locked" in capsys.readouterr().err


@pytest.mark.unit
def test_liquidity_cap():
    panel = make_panel({"AAA": [10_000.0] * 10})
    # ADV value = 1e6 shares x 10,000 = 1e10 VND; 1% cap -> 10,000 shares.
    res = run(Scripted({0: lambda ctx: [ctx.buy("AAA", weight=0.9)]}), "2019-01-01", "2019-12-31",
              panel=panel, universe_kind="vn30_today", max_adv_pct=0.01)
    assert res.fills.iloc[0].qty == 10_000


@pytest.mark.unit
def test_slippage_multiplier_restored():
    panel = make_panel({"AAA": [10_000.0] * 10})
    base = vn_rules.SLIPPAGE_PCT
    res = run(Scripted({0: lambda ctx: [ctx.buy("AAA", weight=0.5)]}), "2019-01-01", "2019-12-31",
              panel=panel, universe_kind="vn30_today", slippage_mult=3.0)
    assert res.fills.iloc[0].price == vn_rules.ceil_to_tick(10_000 * (1 + 3 * base / 100), "HOSE")
    assert base == vn_rules.SLIPPAGE_PCT

    def boom(ctx):
        raise RuntimeError("strategy failed")

    with pytest.raises(RuntimeError):
        run(boom, "2019-01-01", "2019-12-31", panel=panel, slippage_mult=5.0)
    assert base == vn_rules.SLIPPAGE_PCT


@pytest.mark.unit
def test_index_buy_and_hold_is_the_benchmark():
    panel = make_panel({"AAA": [10_000.0] * 20})
    res = run_index("2019-01-01", "2019-12-31", panel=panel)
    assert res.daily["nav"].iloc[-1] == pytest.approx(1e9 * 1100 / 1000)
    assert res.metrics == res.benchmark
    strat = run(lambda ctx: None, "2019-01-01", "2019-12-31", panel=panel)
    assert strat.benchmark["total_return_pct"] == res.metrics["total_return_pct"] == 10.0


@pytest.mark.unit
def test_the_command_line_prints_and_writes_a_run(monkeypatch, tmp_path, capsys):
    from research.cli import main

    panel = make_panel({"AAA": [10_000 + 10 * i for i in range(40)]})
    monkeypatch.setattr(rdata, "load_panel", lambda rebuild=False, unlock_test=False: panel)
    out = tmp_path / "run"
    assert main(["run", "buyhold:AAA", "--start", "2019-01-01", "--end", "2019-12-31", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "CAGR %" in text and "Calendar-year returns" in text and "buyhold_AAA" in text
    assert {p.name for p in out.iterdir()} == {"summary.json", "nav.csv", "fills.csv", "yearly.csv"}
    assert len(pd.read_csv(out / "fills.csv")) == 1
    assert len(pd.read_csv(out / "nav.csv")) == 40


@pytest.mark.unit
def test_the_command_line_rejects_an_unknown_symbol(monkeypatch, capsys):
    from research.cli import main

    panel = make_panel({"AAA": [10_000.0] * 40})
    monkeypatch.setattr(rdata, "load_panel", lambda rebuild=False, unlock_test=False: panel)
    assert main(["run", "buyhold:ZZZZ", "--start", "2019-01-01", "--end", "2019-12-31"]) == 1
    assert "unknown symbol 'ZZZZ'" in capsys.readouterr().err


@pytest.mark.unit
def test_baselines_without_data_exits_cleanly(monkeypatch, tmp_path, capsys):
    from research.cli import main

    monkeypatch.setattr(rdata, "_FULL", None)
    monkeypatch.setattr(rdata, "OHLCV_DIR", tmp_path / "ohlcv")
    monkeypatch.setattr(rdata, "PANEL_CACHE", tmp_path / "research_panel.pkl")
    out = tmp_path / "out"
    assert main(["baselines", "--no-quant", "--out", str(out)]) == 1
    assert "no research data" in capsys.readouterr().err and not out.exists()


@pytest.mark.unit
def test_the_universe_cache_never_serves_another_panel():
    import gc

    from research.universe import universe

    # Two liquidity orders, alternated; each panel is dropped before the next is
    # built, so CPython hands the new panel the old one's id.
    for k in range(40):
        vols = [4e6, 3e6, 2e6, 1e6] if k % 2 == 0 else [1e6, 2e6, 3e6, 4e6]
        p = make_panel({s: [10_000.0] * 300 for s in "ABCD"})
        p.value = p.close * pd.DataFrame(dict(zip("ABCD", vols, strict=True)), index=p.dates)
        expected = ["A", "B"] if k % 2 == 0 else ["D", "C"]
        assert universe(p.dates[-1], "liq_top2", p) == expected
        del p
        gc.collect()


# ── the downloader's incremental update (vendors faked) ──────────────────────


def _bars(days, closes):
    return pd.DataFrame({"Date": days, "Open": closes, "High": closes, "Low": closes, "Close": closes,
                         "Volume": [1e5] * len(days)})


@pytest.mark.unit
def test_download_appends_new_sessions_and_refetches_a_readjusted_history(monkeypatch, tmp_path):
    from research import download

    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-09-01", periods=30)]
    path = tmp_path / "AAA.csv"
    _bars(days[:25], [100.0] * 25).to_csv(path, index=False)
    calls = []

    def fake(symbol, end, start=config.HISTORY_START):
        calls.append(start)
        rows = [d for d in days if start <= d <= end]
        return _bars(rows, [100.0] * len(rows)), "vci"

    monkeypatch.setattr(download, "fetch_bars", fake)
    bars, _ = download.update_bars("AAA", path, days[-1])
    assert list(bars["Date"]) == days and calls == [download._overlap_start(days[24])]
    assert download.update_bars("AAA", path, days[24]) == (None, "up to date")

    def readjusted(symbol, end, start=config.HISTORY_START):
        calls.append(start)
        rows = [d for d in days if start <= d <= end]
        return _bars(rows, [90.0] * len(rows)), "vci"          # a dividend moved the whole history

    monkeypatch.setattr(download, "fetch_bars", readjusted)
    calls.clear()
    bars, source = download.update_bars("AAA", path, days[-1])
    assert calls[-1] == config.HISTORY_START and "re-adjusted" in source
    assert list(bars["Close"]) == [90.0] * 30


@pytest.mark.unit
def test_an_empty_file_left_by_a_killed_run_is_fetched_again(monkeypatch, tmp_path):
    from research import download

    days = ["2026-10-13", "2026-10-14"]
    monkeypatch.setattr(download, "fetch_bars", lambda symbol, end, start=None: (_bars(days, [100.0, 101.0]), "vci"))
    monkeypatch.setattr(download, "fetch_price_history", lambda symbol, start, end: pd.DataFrame(
        {"Date": days, "RawClose": [100.0, 101.0], "AdjClose": [100.0, 101.0]}))
    empty = tmp_path / "AAA.csv"
    empty.write_text("")
    bars, _ = download.update_bars("AAA", empty, days[-1])
    assert list(bars["Date"]) == days
    assert list(download.update_price_history("AAA", empty, days[-1])["Date"]) == days


@pytest.mark.unit
def test_a_failed_symbol_leaves_no_files(monkeypatch, tmp_path):
    from research import download

    monkeypatch.setattr(download, "OHLCV_DIR", tmp_path)
    monkeypatch.setattr(download, "PH_DIR", tmp_path)
    monkeypatch.setattr(download, "fetch_price_history", lambda *a: pd.DataFrame(columns=list(download.PH_FIELDS.values())))

    def nothing(*a, **k):
        raise RuntimeError("no data")

    monkeypatch.setattr(download, "fetch_bars", nothing)
    monkeypatch.setattr(download, "fetch_vndirect", nothing)
    info = download.download_symbol("XYZQ", "2026-10-14", refresh=False)
    assert not info["ok"] and list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_ctrl_c_cancels_the_queued_downloads():
    import threading
    import time as _time

    from research import download

    ran = []
    lock = threading.Lock()

    def task(x):
        _time.sleep(0.02)
        with lock:
            ran.append(x)

    def handle(_x, _fut):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        download._run_pool(task, range(40), 2, handle)
    assert len(ran) < 10            # the queue was dropped, not drained


@pytest.fixture
def fake_vendors(monkeypatch, tmp_path):
    """download.main() against a temp data directory with every vendor call faked."""
    from research import download

    for name, rel in (("DATA_DIR", ""), ("OHLCV_DIR", "ohlcv"), ("PH_DIR", "ph"), ("META_FILE", "download_meta.json"),
                      ("LISTING_FILE", "listing.json"), ("VN30_FILE", "vn30.json"), ("PROBE_FILE", "probe.json"),
                      ("SNAP_FILE", "hose_monthly.csv"), ("LOG_FILE", "download.log")):
        monkeypatch.setattr(download, name, tmp_path / rel if rel else tmp_path)
    monkeypatch.setattr(download, "_setup_logging", lambda: None)
    today = {"end": "2026-10-21"}
    monkeypatch.setattr(download, "_today_final", lambda: today["end"])
    monkeypatch.setattr(download, "_retry", lambda fn, *a, **k: [] if "listing" in fn.__name__ else ["FPT"])
    snapshots = []

    def fetch_snapshots(days, workers):
        snapshots.append(days)
        return pd.DataFrame({"Date": ["2026-10-01"] * 2, "Code": ["FPT", "VNM"]}), []

    def download_symbol(symbol, end, refresh, frozen=False):
        if symbol == "XYZQ":
            return {"symbol": symbol, "ok": False, "error": "RuntimeError: no data"}
        _bars(["2026-10-14", end], [1.0, 1.0]).to_csv(tmp_path / "ohlcv" / f"{symbol}.csv", index=False)
        return {"symbol": symbol, "ok": True, "last_date": end}

    monkeypatch.setattr(download, "fetch_snapshots", fetch_snapshots)
    monkeypatch.setattr(download, "download_symbol", download_symbol)

    def meta():
        return json.loads((tmp_path / "download_meta.json").read_text())

    return download, today, snapshots, meta


@pytest.mark.unit
def test_a_partial_download_keeps_the_data_end_and_forgets_old_typos(fake_vendors):
    download, today, snapshots, meta = fake_vendors
    today["end"] = "2026-10-14"
    assert download.main([]) == 0                                   # full run: the calendar moves
    assert meta()["data_end"] == "2026-10-14" and len(snapshots) == 1

    today["end"] = "2026-10-21"
    assert download.main(["--symbols", "FPT,xyzq"]) == 0
    m = meta()
    assert m["data_end"] == "2026-10-14"                            # VNM was not updated: no NaN week for it
    assert m["failed"] == ["XYZQ"] and len(snapshots) == 1          # --symbols skips the snapshots

    assert download.main(["--symbols", "FPT"]) == 0
    assert meta()["failed"] == []                                   # not requested, no files: forgotten

    assert download.main([]) == 0
    assert meta()["data_end"] == "2026-10-21"


# ── the bot's defensive book through the harness ─────────────────────────────


def _factor_panel(m) -> Panel:
    """tests/test_factor.py's synthetic market as a harness panel (bars as its ``_bar`` builds them)."""
    from tests.test_factor import _full

    has = m.close.notna()
    exchange = pd.DataFrame({s: [m.exchange[s]] * len(m.dates) for s in m.symbols}, index=m.dates)
    history = {}
    for s in m.symbols:
        _, hist = _full(m, s)
        idx = pd.to_datetime(list(hist))
        history[s] = pd.DataFrame(list(hist.values()), index=idx, columns=["MatchValue", "RawClose", "Shares"])
    return Panel(
        dates=m.dates, open=m.close.copy(), high=m.close * 1.01, low=m.close * 0.99, close=m.close,
        volume=m.volume, raw_close=m.raw.where(m.raw > 0).where(has),
        value=m.match.fillna(m.close * m.volume).where(has), exchange=exchange, vnindex=m.vnindex,
        vn30=m.vnindex.copy(), vnindex_bars=pd.DataFrame({"Close": m.vnindex}), vn30_today=[],
        price_history=history,
    )


@pytest.fixture(scope="module")
def factor_market():
    from tests.test_factor import _market

    return _market()


@pytest.mark.unit
def test_the_factor_strategy_trades_what_test_factor_expects(factor_market):
    """The harness "factor" strategy (live module, bot's session loop) == test_factor's
    port of the evaluated research code and its replay of the live module."""
    from research.factor_book import DefensiveBook
    from tests.test_factor import N_SESSIONS, REPLAY_FROM, Harness, _fills, _live_run

    m = factor_market
    panel = _factor_panel(m)
    book = DefensiveBook()
    res = run(book, m.days[REPLAY_FROM], m.days[N_SESSIONS - 1], panel=panel, unlock_test=True)
    ref, ref_baskets = Harness(m).run(REPLAY_FROM, N_SESSIONS - 1)
    live, _ = _live_run(m, REPLAY_FROM, N_SESSIONS - 1)
    got = _fills(res.ledger)
    assert len(got) > 40
    assert got == _fills(ref) == _fills(live)
    assert [h["nav"] for h in res.ledger.history] == [h["nav"] for h in ref.history]
    assert res.daily["nav"].iloc[-1] == pytest.approx(ref.nav(), abs=1e-6)
    assert res.universe_kind == "cap50"
    # The universe the live build_universe gave on each rebuild is the research port's.
    h = Harness(m)
    for day, names in book.universes.items():
        assert names == h.universe(m.days.index(day)), day


@pytest.mark.unit
def test_the_factor_strategy_with_other_thresholds(factor_market):
    from dataclasses import replace

    from research.factor_book import DefensiveBook
    from tests.test_factor import N_SESSIONS, REPLAY_FROM, Harness, _fills
    from tradingagents.quant import params as qp

    m = factor_market
    p = replace(qp.DEFAULT, fb_top_k=6, fb_buffer=2.0, fb_market_ma=150, fb_universe=30)
    res = run(DefensiveBook(p), m.days[REPLAY_FROM], m.days[N_SESSIONS - 1], panel=_factor_panel(m),
              unlock_test=True)
    ref, _ = Harness(m, p).run(REPLAY_FROM, N_SESSIONS - 1)
    assert _fills(res.ledger) == _fills(ref)


@pytest.mark.unit
def test_strategies_by_name():
    from research.factor_book import DefensiveBook
    from research.strategies import (
        STRATEGIES,
        BuyHoldSymbol,
        EqualWeightRebalance,
        RepoMomentum,
        make,
    )

    assert isinstance(make("factor"), DefensiveBook)
    assert make("factor", next_day="guess").next_day == "guess"
    assert isinstance(make("momentum"), RepoMomentum)
    assert isinstance(make("ew"), EqualWeightRebalance)
    assert make("vnindex") is None
    assert make("buyhold:fpt").symbol == "FPT" and isinstance(make("buyhold:FPT"), BuyHoldSymbol)
    assert {"factor", "momentum", "ew", "quant", "vnindex"} <= set(STRATEGIES)
    with pytest.raises(ValueError):
        make("nope")


# ── on the downloaded data ───────────────────────────────────────────────────

needs_data = pytest.mark.skipif(not config.data_ready(), reason=f"no research data in {config.DATA_DIR}")


@pytest.mark.integration
@needs_data
def test_real_data_load_panel_is_locked():
    assert rdata.load_panel().dates[-1] <= pd.Timestamp(config.LOCK_AFTER)


@pytest.mark.integration
@needs_data
def test_real_data_buy_and_hold_fpt():
    """On real data: buy & hold FPT through 2019 tracks its close-to-close return minus costs."""
    panel = rdata.load_panel()
    if "FPT" not in panel.close.columns:
        pytest.skip("FPT not downloaded")
    res = run(Scripted({0: lambda ctx: [ctx.buy("FPT", weight=0.98)]}), "2019-01-01", "2019-12-31",
              universe_kind="vn30_today", panel=panel)
    c = panel.close["FPT"].loc["2019-01-01":"2019-12-31"]
    o = panel.open["FPT"].loc["2019-01-01":"2019-12-31"]
    buy = res.fills.iloc[0]
    stock_ret = c.iloc[-1] / o.iloc[1] - 1
    pos_value = buy.qty * c.iloc[-1]
    cost = buy.qty * buy.price * (1 + FEE)
    assert res.daily["nav"].iloc[-1] == pytest.approx(1e9 - cost + pos_value, abs=2)
    assert (pos_value / cost - 1) == pytest.approx(stock_ret, abs=0.004)   # buy fee + slippage only (unrealised)


@pytest.mark.integration
@needs_data
def test_real_data_repo_momentum_matches_repo_momentum_run():
    """The harness driving momentum.decide/plan == the repo's own momentum.run on the same bars."""
    from types import SimpleNamespace

    from research.backtest import _Arrays
    from research.strategies import RepoMomentum
    from tradingagents.quant.params import DEFAULT
    from tradingagents.trading import momentum

    panel = rdata.load_panel()
    arrays = _Arrays(panel)
    syms = panel.vn30_today
    bars = {s: {} for s in syms}
    for i in range(len(panel.dates)):
        for s in syms:
            b = arrays.bar(s, i)
            if b:
                bars[s][b.date] = b
    data = SimpleNamespace(dates=arrays.dates, bars=bars,
                           benchmark={d: float(v) for d, v in zip(arrays.dates, panel.vnindex, strict=True)})
    ledger = momentum.run(data, DEFAULT, start="2021-01-01", end="2023-12-31")
    res = run(RepoMomentum(), "2021-01-01", "2023-12-31", universe_kind="vn30_today", panel=panel)
    assert res.daily["nav"].iloc[-1] == pytest.approx(ledger.history[-1]["nav"], abs=1)
    assert len(res.fills) == len(ledger.fills)


@pytest.mark.integration
@needs_data
def test_real_data_reproduces_the_published_valid_numbers():
    """README / research/RESULTS.md: the bot's rules on VALID 13.86% / -12.0%; the momentum
    rotation on liq_top30 13.57% / -48.82% (data downloaded on 2026-10-15)."""
    from research.factor_book import DefensiveBook
    from research.strategies import RepoMomentum

    panel = rdata.load_panel()
    if panel.info.get("data_end") != "2026-10-14":
        pytest.skip(f"numbers published for data to 2026-10-14, this data ends {panel.info.get('data_end')}")
    fb = run(DefensiveBook(), *config.VALID, panel=panel)
    assert (fb.metrics["cagr_pct"], round(fb.metrics["max_drawdown_pct"], 1)) == (13.86, -12.0)
    mom = run(RepoMomentum(), *config.VALID, universe_kind="liq_top30", panel=panel)
    assert (mom.metrics["cagr_pct"], mom.metrics["max_drawdown_pct"]) == (13.57, -48.82)
    assert not math.isnan(fb.benchmark["cagr_pct"])
