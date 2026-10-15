"""The defensive book (trading/factor.py) makes the research harness's decisions.

The strategy was chosen by an independent evaluation run through a research
harness (research/harness + research/trend-index/ti.py, ``FactorBook``
"lvhi52_cap50_sma200"), outside this repo. ``Harness`` below is a line-by-line
port of the parts that decide: the point-in-time liquidity universe
(``harness.universe``), the market-cap ranking (``ti.cap_panel`` /
``ti.universe``), the score and basket (``FactorBook.scores/basket/on_session``
with ``check="W"``, ``rebalance="M"``), the order generation
(``Context.orders_for_weights``) and the session loop (``harness.backtest.run``)
on the repo's ledger. One line differs on purpose: equal scores are ordered by
lower volatility (the evaluator's recommendation) instead of pandas' unstable
quicksort order.

A synthetic market (trends, a stock dividend, suspensions, an IPO, a penny
stock, an HNX stock, missing price-history rows, VNINDEX crossing its 200-session
average several times) is replayed through the port, through the live module
and through the bot itself (TradingBot.catch_up, session by session, with the
vendors faked from the same data): the fills must be identical.
"""

from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradingagents.market import vn_rules
from tradingagents.quant import params as qp
from tradingagents.trading import factor
from tradingagents.trading.ledger import Bar, Ledger, Order

N_SESSIONS = 640
N_STOCKS = 64
REPLAY_FROM = 300            # sessions before: history for the 250-bar screen and the 252-session score


# ── a synthetic market ───────────────────────────────────────────────────────


def _calendar(n: int) -> pd.DatetimeIndex:
    days = pd.bdate_range("2022-06-01", periods=n + 40)
    days = days[[vn_rules.is_session_day(d) for d in days.strftime("%Y-%m-%d")]]
    return days[:n]


def _market(seed: int = 7) -> SimpleNamespace:
    rng = np.random.default_rng(seed)
    dates = _calendar(N_SESSIONS)
    t = np.arange(N_SESSIONS)
    # VNINDEX: a trend with slow swings around it, crossing its SMA200 several times.
    vni = 1000 * np.exp(0.0002 * t + 0.15 * np.sin(t / 25) + np.cumsum(rng.normal(0, 0.004, N_SESSIONS)))
    market_ret = np.diff(np.log(vni), prepend=np.log(vni[0]))
    symbols = [f"S{i:02d}" for i in range(N_STOCKS)]
    close, volume, match, raw, shares = {}, {}, {}, {}, {}
    for k, sym in enumerate(symbols):
        vol = rng.uniform(0.008, 0.03)
        beta = rng.uniform(0.5, 1.5)
        drift = rng.normal(0.0003, 0.0006)
        ret = beta * market_ret + drift + rng.normal(0, vol, N_SESSIONS)
        # Coarse prices: many equal closes, so the 52-week-high ratio often ties at 1.0.
        price = np.round(rng.uniform(8_000, 90_000) * np.exp(np.cumsum(ret)), -2)
        if k == 3:
            price = price / 6                                   # a penny stock (under 5,000đ)
        c = pd.Series(price, index=dates)
        v = pd.Series(np.round(rng.lognormal(13 + 2 * rng.random(), 0.5, N_SESSIONS), -2), index=dates)
        if k == 5:
            c.iloc[: 420] = np.nan                              # listed late: under 250 bars for a while
        if k % 9 == 4:
            gaps = rng.choice(N_SESSIONS, 25, replace=False)    # suspensions: no bar
            c.iloc[gaps] = np.nan
        if k % 11 == 2:
            c.iloc[400:430] = np.nan                            # a long suspension
        has = c.notna()
        v = v.where(has)
        # Unadjusted closes: a 20% stock dividend on session 450 adjusted the history before it.
        r = c.copy()
        if k % 7 == 1:
            r.iloc[:450] = c.iloc[:450] * 1.2
        mv = (r * v * rng.uniform(0.95, 1.05, N_SESSIONS)).where(has)
        mv[rng.random(N_SESSIONS) < 0.03] = np.nan             # matched value missing: close x volume
        sh = pd.Series(rng.uniform(1e8, 3e9), index=dates)
        sh.iloc[300:] *= rng.choice([1.0, 1.0, 1.5])
        sh[rng.random(N_SESSIONS) < 0.02] = np.nan              # share count missing on some rows
        ph_row = has & (rng.random(N_SESSIONS) > 0.02)          # price-history rows missing on some sessions
        close[sym], volume[sym] = c, v
        match[sym], raw[sym], shares[sym] = mv.where(ph_row), r.where(ph_row), sh.where(ph_row)
    exchange = {s: ("HNX" if s == "S07" else "HOSE") for s in symbols}
    return SimpleNamespace(dates=dates, days=[d.strftime("%Y-%m-%d") for d in dates], symbols=symbols,
                           close=pd.DataFrame(close), volume=pd.DataFrame(volume), match=pd.DataFrame(match),
                           raw=pd.DataFrame(raw), shares=pd.DataFrame(shares), vnindex=pd.Series(vni, index=dates),
                           exchange=exchange)


@pytest.fixture(scope="module")
def market():
    return _market()


def _bar(m, sym: str, i: int) -> Bar | None:
    c = m.close[sym]
    if math.isnan(c.iloc[i]):
        return None
    prev = c.iloc[:i].dropna()
    price = float(c.iloc[i])
    return Bar(m.days[i], price, price * 1.01, price * 0.99, price,
               float(prev.iloc[-1]) if len(prev) else None, exchange=m.exchange[sym],
               volume=float(m.volume[sym].iloc[i]), prev_date=prev.index[-1].strftime("%Y-%m-%d") if len(prev) else None)


# ── the harness, ported ──────────────────────────────────────────────────────


class Harness:
    """research harness + ti.FactorBook("lvhi52_cap50_sma200"), pandas as written there."""

    def __init__(self, m, params: qp.Params = qp.DEFAULT):
        self.m, self.p = m, params
        has = m.close.notna()
        # harness.data: value = MatchValue where present, else close x volume, on bar sessions only.
        value = m.match.fillna(m.close * m.volume).where(has)
        raw_close = m.raw.where(m.raw > 0).where(has)
        exchange = pd.DataFrame({s: [m.exchange[s]] * len(m.dates) for s in m.symbols}, index=m.dates)
        # harness.universe._prep
        nbars = has.cumsum()
        listed = nbars > 0
        val = value.where(has, 0.0).where(listed)
        med = val.rolling(60, min_periods=30).median()
        raw = raw_close.where(raw_close.notna(), m.close.where(raw_close.isna() & has))
        raw_last = raw.ffill()
        recent = has.astype(float).rolling(5, min_periods=1).max() > 0
        hose = exchange == "HOSE"
        self.eligible = (hose & (nbars >= 250) & (raw_last >= 5_000.0) & recent & med.notna() & (med > 0)).to_numpy()
        self.med = med.to_numpy()
        self.cols = np.array(m.close.columns)
        # ti.cap_panel: Shares x RawClose (> 0), carried forward 5 sessions.
        self.cap = (m.shares * m.raw).where(lambda x: x > 0).ffill(limit=5)

    def liq(self, i: int, n: int) -> list[str]:
        ok = self.eligible[i]
        med = np.where(ok, self.med[i], -np.inf)
        order = np.argsort(-med, kind="stable")[:n]
        return [str(self.cols[j]) for j in order if ok[j]]

    def universe(self, i: int) -> list[str]:
        liq100 = set(self.liq(i, 100))
        cap = self.cap.iloc[i]
        cap = cap[cap.index.isin(liq100)].dropna()
        return list(cap.nlargest(self.p.fb_universe).index)

    def scores(self, i: int, syms: list[str]) -> pd.Series:
        px = self.m.close[syms].iloc[: i + 1].iloc[-252:]
        lr = np.log(px).diff()
        vol = lr.std(skipna=True)
        cnt = lr.notna().sum()
        vol = vol[cnt >= 150]
        hi = (px.iloc[-1] / px.max()).dropna()
        common = vol.index.intersection(hi.index)
        sc = (-vol[common]).rank() + hi[common].rank()
        # The one deliberate change: ties by lower volatility (harness: sc.sort_values(ascending=False)).
        frame = pd.DataFrame({"sc": sc, "vol": vol[common], "pos": range(len(common))})
        return frame.sort_values(["sc", "vol", "pos"], ascending=[False, True, True])["sc"]

    def basket(self, i: int, positions) -> dict:
        syms = [s for s in self.universe(i) if not math.isnan(self.m.close[s].iloc[i])]
        ranked = list(self.scores(i, syms).index)
        k, buffer = self.p.fb_top_k, self.p.fb_buffer
        held = [s for s in positions if s in ranked[: int(round(k * buffer))]]
        held = held[:k]
        pick = list(held)
        for s in ranked:
            if len(pick) >= k:
                break
            if s not in pick:
                pick.append(s)
        return dict.fromkeys(pick, 0.98 / k)

    def _end(self, i: int, key) -> bool:
        if i + 1 >= len(self.m.dates):
            return True
        return key(self.m.dates[i + 1]) != key(self.m.dates[i])

    def on_session(self, i: int, state: dict, positions) -> dict:
        if self._end(i, lambda d: d.isocalendar()[:2]) or "on" not in state:
            s = self.m.vnindex.iloc[: i + 1].dropna()
            L = self.p.fb_market_ma
            state["on"] = False if len(s) < L else float(s.iloc[-1]) > float(s.iloc[-L:].mean())
        if not state["on"]:
            state["basket"] = None
            return {}
        if state.get("basket") is None or self._end(i, lambda d: (d.year, d.month)):
            state["basket"] = self.basket(i, positions)
        return state["basket"]

    def orders_for_weights(self, led: Ledger, i: int, weights: dict) -> list[Order]:
        day = self.m.days[i]

        def last_close(sym):
            v = self.m.close[sym].iloc[i]
            return None if math.isnan(v) else float(v)

        targets = {s: w for s, w in weights.items() if w and w > 0}
        open_sells = {o.symbol for o in led.orders if o.side == "SELL"}
        open_buys = {o.symbol for o in led.orders if o.side == "BUY"}
        orders, proceeds = [], 0.0
        for sym, pos in led.positions.items():
            if sym not in targets and sym not in open_sells:
                orders.append(Order(sym, "SELL", pos.qty, None, day, "rebalance"))
                proceeds += pos.qty * (last_close(sym) or pos.last_price)
        nav = led.nav()
        available = led.cash - led.reserved() + proceeds * 0.997
        for sym, w in sorted(targets.items(), key=lambda kv: -kv[1]):
            if sym in led.positions or sym in open_buys:
                continue
            price = last_close(sym)
            if not price:
                continue
            budget = min(nav * w, available)
            qty = vn_rules.round_lot(budget / (price * (1 + vn_rules.BUY_FEE_PCT / 100) * 1.02))
            if qty <= 0:
                continue
            orders.append(Order(sym, "BUY", qty, None, day, "rebalance", ref=price))
            available -= qty * price * 1.02
        return orders

    def run(self, first: int, last: int):
        """harness.backtest.run over sessions [first, last]: (ledger, {day: basket})."""
        led = Ledger(cash=1e9, start_cash=1e9)
        state: dict = {}
        baskets = {}
        for i in range(first, last + 1):
            active = {o.symbol for o in led.orders} | set(led.positions)
            bars = {s: b for s in active if (b := _bar(self.m, s, i)) is not None}
            led.process_session(bars, rules=factor.RULES)
            led.end_of_session(self.m.days[i], float(self.m.vnindex.iloc[i]))
            weights = self.on_session(i, state, led.positions)
            baskets[self.m.days[i]] = list(weights)
            led.orders.extend(self.orders_for_weights(led, i, weights))
        return led, baskets


# ── the live module on the same data ─────────────────────────────────────────


_FULL: dict = {}


def _full(m, sym: str) -> tuple[dict, dict]:
    """A stock's whole bar and price-history series as the vendors' loaders give them."""
    key = (id(m), sym)
    if key not in _FULL:
        c, v = m.close[sym], m.volume[sym]
        keep = c.notna()
        bars = {d.strftime("%Y-%m-%d"): (float(x), float(y))
                for d, x, y in zip(c.index[keep], c[keep], v[keep], strict=True)}
        rows = (m.raw[sym].notna() | m.shares[sym].notna() | m.match[sym].notna()).to_numpy()
        hist = {d.strftime("%Y-%m-%d"): (float(a), float(b), float(c))
                for d, a, b, c in zip(m.dates[rows], m.match[sym][rows], m.raw[sym][rows], m.shares[sym][rows],
                                      strict=True)}
        _FULL[key] = (bars, hist)
    return _FULL[key]


def _bars_dict(m, sym: str, i: int) -> dict:
    day = m.days[i]
    return {d: x for d, x in _full(m, sym)[0].items() if d <= day}


def _history_dict(m, sym: str, i: int) -> dict:
    day = m.days[i]
    return {d: x for d, x in _full(m, sym)[1].items() if d <= day}


def _live_run(m, first: int, last: int, params: qp.Params = qp.DEFAULT):
    led = Ledger(cash=1e9, start_cash=1e9)
    baskets = {}
    for i in range(first, last + 1):
        day = m.days[i]
        active = {o.symbol for o in led.orders} | set(led.positions)
        bars = {s: b for s in active if (b := _bar(m, s, i)) is not None}
        led.process_session(bars, rules=factor.RULES)
        led.end_of_session(day, float(m.vnindex.iloc[i]))
        sessions = m.days[: i + 1]

        def ranking(i=i, day=day, sessions=sessions):
            snaps = [factor.snapshot(s, sessions, _bars_dict(m, s, i), _history_dict(m, s, i),
                                     hose=m.exchange[s] == "HOSE") for s in m.symbols]
            names = [s for s in factor.cap_universe(snaps, params.fb_universe) if day in _bars_dict(m, s, i)]
            window = sessions[-factor.SCORE_WINDOW:]
            closes = {s: factor.aligned_closes({d: x[0] for d, x in _bars_dict(m, s, i).items()}, window)
                      for s in names}
            return factor.rank(closes, names)

        next_day = m.days[i + 1] if i + 1 < len(m.days) else None
        decision = factor.decide(led, day, next_day, list(m.vnindex.iloc[: i + 1]), params, ranking)
        baskets[day] = list(decision.basket)
        last_close = {s: float(m.close[s].iloc[i]) for s in set(decision.basket) | set(led.positions)
                      if not math.isnan(m.close[s].iloc[i])}
        factor.plan(led, day, decision, last_close, params)
    return led, baskets


def _fills(led: Ledger) -> list[tuple]:
    return [(f.date, f.symbol, f.side, f.qty, round(f.price, 6)) for f in led.fills]


# ── tests ────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_synthetic_market_exercises_the_rules(market):
    ref, baskets = Harness(market).run(REPLAY_FROM, N_SESSIONS - 1)
    on = [bool(b) for b in baskets.values()]
    flips = sum(1 for a, b in zip(on, on[1:], strict=False) if a != b)
    assert flips >= 4 and any(on) and not all(on)                    # the market filter turns on and off
    sold = [f for f in ref.fills if f.side == "SELL"]
    assert len(ref.fills) > 40 and sold                               # rebuilds trade
    h = Harness(market)
    i = N_SESSIONS - 1
    uni = h.universe(i)
    assert len(uni) == 50 and "S07" not in uni and "S03" not in uni  # HNX and penny stocks never enter
    s05 = factor.snapshot("S05", market.days[:450], _bars_dict(market, "S05", 449), _history_dict(market, "S05", 449))
    assert not s05.eligible                                           # 30 bars: too young


@pytest.mark.unit
def test_the_universe_matches_the_harness_on_every_month_end(market):
    h = Harness(market)
    ends = [i for i in range(REPLAY_FROM, N_SESSIONS - 1) if market.days[i][:7] != market.days[i + 1][:7]]
    for i in ends:
        sessions = market.days[: i + 1]
        snaps = [factor.snapshot(s, sessions, _bars_dict(market, s, i), _history_dict(market, s, i),
                                 hose=market.exchange[s] == "HOSE") for s in market.symbols]
        assert [s.symbol for s in factor.liquid_pool(snaps)] == h.liq(i, 100), market.days[i]
        assert factor.cap_universe(snaps, 50) == h.universe(i), market.days[i]


@pytest.mark.unit
def test_the_live_module_makes_the_harness_decisions(market):
    ref, ref_baskets = Harness(market).run(REPLAY_FROM, N_SESSIONS - 1)
    live, live_baskets = _live_run(market, REPLAY_FROM, N_SESSIONS - 1)
    assert live_baskets == ref_baskets
    assert _fills(live) == _fills(ref)
    assert [h["nav"] for h in live.history] == [h["nav"] for h in ref.history]


@pytest.mark.unit
def test_other_thresholds_match_too(market):
    p = replace(qp.DEFAULT, fb_top_k=6, fb_buffer=2.0, fb_market_ma=150, fb_universe=30)
    ref, ref_baskets = Harness(market, p).run(REPLAY_FROM, N_SESSIONS - 1)
    live, live_baskets = _live_run(market, REPLAY_FROM, N_SESSIONS - 1, p)
    assert live_baskets == ref_baskets and _fills(live) == _fills(ref)


@pytest.mark.unit
def test_the_bot_trades_the_harness_decisions_session_by_session(market, settings, monkeypatch):
    """TradingBot.catch_up, one session at a time, vendors faked from the market."""
    from tradingagents.dataflows import money_flows, ohlcv
    from tradingagents.dataflows.vendors import vci
    from tradingagents.quant import engine
    from vnbot import trader as trader_module
    from vnbot.service import Watchlist

    m = market
    frames = {}
    for sym in m.symbols:
        c = m.close[sym]
        keep = c.notna()
        frames[sym] = pd.DataFrame({"Date": m.dates[keep], "Open": c[keep], "High": c[keep] * 1.01,
                                    "Low": c[keep] * 0.99, "Close": c[keep], "Volume": m.volume[sym][keep]})
    frames["VNINDEX"] = pd.DataFrame({"Date": m.dates, "Open": m.vnindex, "High": m.vnindex, "Low": m.vnindex,
                                      "Close": m.vnindex, "Volume": 1e9})
    history = {}
    for sym in m.symbols:
        rows = m.raw[sym].notna() | m.shares[sym].notna() | m.match[sym].notna()
        history[sym] = pd.DataFrame({"Date": m.dates[rows.to_numpy()], "MatchValue": m.match[sym][rows],
                                     "Close": m.raw[sym][rows], "TotalShares": m.shares[sym][rows]})

    def load(symbol, as_of, fill_gaps=True, sources=None):
        frame = frames[symbol]
        return frame[frame["Date"] <= pd.Timestamp(as_of)].reset_index(drop=True)

    guards = set()

    def price_history(symbol, as_of, guarded=True):
        guards.add(guarded)
        frame = history[symbol]
        return frame[frame["Date"] <= pd.Timestamp(as_of)].reset_index(drop=True)

    monkeypatch.setattr(ohlcv, "load_ohlcv", load)
    monkeypatch.setattr(money_flows, "load_price_history", price_history)
    monkeypatch.setattr(money_flows, "index_members", lambda group="VN30": ["S10", "S11"])
    monkeypatch.setattr(vci, "fetch_listing", lambda: [
        {"symbol": s, "board": "HNX" if m.exchange[s] == "HNX" else "HSX", "type": "STOCK"} for s in m.symbols]
        + [{"symbol": "E1VFVN30", "board": "HSX", "type": "ETF"}])
    monkeypatch.setattr(engine, "analyze", lambda s, d, portfolio=None, params=None: SimpleNamespace(
        action="NO_TRADE", symbol=s))
    monkeypatch.setattr(trader_module.TradingBot, "_signals_for", lambda self, *a, **k: ([], {}, []))
    bot = trader_module.TradingBot(settings, Watchlist(settings))
    first, last = 470, 600
    for i in range(first, last + 1):
        bot.catch_up(m.days[i])
    book = bot.factor_book()
    ref, ref_baskets = Harness(m).run(first, last)
    assert len(ref.fills) > 20 and _book_fills(book) == _fills(ref)
    assert book.meta["fb_basket"] == (ref_baskets[m.days[last]] or None)
    assert book.cash == pytest.approx(ref.cash)
    assert bot.last_day() == m.days[last]
    assert guards == {False}                    # the universe build reads VCI without the shared breaker
    assert bot.last_report()["signal_strategy"] == "factor"     # what the next report compares with


def _book_fills(book: Ledger) -> list[tuple]:
    return [(f.date, f.symbol, f.side, f.qty, round(f.price, 6)) for f in book.fills]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from vnbot import settings as settings_module

    for name in ("TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_IDS", "ALLOWED_USER_ID", "WEB_PASSWORD", "WATCHLIST",
                 "PUBLIC_URL", "TELEGRAM_REPORT_CHAT_IDS", "TRADINGAGENTS_RESULTS_DIR", "TRADINGAGENTS_CACHE_DIR",
                 "TRADINGAGENTS_MEMORY_LOG_PATH", "SIGNAL_STRATEGY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADINGAGENTS_PARAMS_PATH", str(tmp_path / "quant_params.json"))
    return settings_module.load()


# ── the rules, one at a time ─────────────────────────────────────────────────


def _closes_up(n=300):
    return [1000.0 + i for i in range(n)]


def _ranked(*names):
    return lambda: [factor.Ranked(s, 100.0 - i, 0.01, 1.0) for i, s in enumerate(names)]


@pytest.mark.unit
def test_the_filter_is_checked_on_week_ends_only():
    book = Ledger(cash=1e9, start_cash=1e9)
    p = replace(qp.DEFAULT, fb_market_ma=10, fb_top_k=2)
    up, down = [100.0] * 9 + [200.0], [100.0] * 9 + [50.0]
    d = factor.decide(book, "2025-03-04", "2025-03-05", up, p, _ranked("A", "B", "C"))   # first session: checked
    assert d.checked and d.market_ok and d.basket == ["A", "B"] and d.rebalanced
    d = factor.decide(book, "2025-03-05", "2025-03-06", down, p, _ranked("C"))           # Wednesday: not checked
    assert not d.checked and d.market_ok and d.basket == ["A", "B"] and not d.rebalanced
    d = factor.decide(book, "2025-03-07", "2025-03-10", down, p, _ranked("C"))           # Friday: checked, off
    assert d.checked and not d.market_ok and d.basket == [] and book.meta["fb_basket"] is None
    d = factor.decide(book, "2025-03-10", "2025-03-11", up, p, _ranked("C", "A"))        # Monday: stays off
    assert not d.market_ok
    d = factor.decide(book, "2025-03-14", "2025-03-17", up, p, _ranked("C", "A"))        # Friday: on, rebuilt
    assert d.market_ok and d.rebalanced and d.basket == ["C", "A"]


@pytest.mark.unit
def test_month_end_rebuilds_keep_held_names_within_the_buffer():
    book = Ledger(cash=1e9, start_cash=1e9)
    p = replace(qp.DEFAULT, fb_market_ma=10, fb_top_k=2, fb_buffer=1.5)
    up = [100.0] * 9 + [200.0]
    factor.decide(book, "2025-03-27", "2025-03-28", up, p, _ranked("A", "B"))
    book.positions = {"B": object(), "A": object()}
    d = factor.decide(book, "2025-03-28", "2025-03-31", up, p, _ranked("C", "D", "B", "A"))   # not a month end
    assert not d.rebalanced
    d = factor.decide(book, "2025-03-31", "2025-04-01", up, p, _ranked("C", "D", "B", "A"))   # month end
    assert d.rebalanced and d.basket == ["B", "C"]          # B ranks 3rd <= round(2 x 1.5): kept; A left
    assert factor.select(["C", "D", "B", "A"], ["A", "B"], 2, 1.0) == ["C", "D"]


@pytest.mark.unit
def test_a_check_missed_by_a_wrong_holiday_guess_runs_late():
    book = Ledger(cash=1e9, start_cash=1e9)
    p = replace(qp.DEFAULT, fb_market_ma=10, fb_top_k=1)
    up, down = [100.0] * 9 + [200.0], [100.0] * 9 + [50.0]
    factor.decide(book, "2025-01-27", "2025-01-28", up, p, _ranked("A"))
    # Thursday, guessed to be followed by Friday - but Friday was a (lunar) holiday.
    d = factor.decide(book, "2025-01-30", factor.next_session_guess("2025-01-30"), down, p, _ranked("A"))
    assert not d.checked and d.market_ok
    d = factor.decide(book, "2025-02-03", "2025-02-04", down, p, _ranked("A"))     # Monday: the week's check, late
    assert d.checked and not d.market_ok
    assert factor.next_session_guess("2025-04-29") == "2025-05-02"                  # 30/4 and 1/5 are known


@pytest.mark.unit
def test_the_next_session_is_guessed_from_the_exchange_calendar():
    # Tết 2026 (16-20/2): the Friday before is the week's last session.
    assert factor.next_session_guess("2026-02-13") == "2026-02-23"
    # 31/8-2/9/2026: 28/8 is August's last session.
    assert factor.next_session_guess("2026-08-28") == "2026-09-03"
    assert factor.next_session_guess("2027-02-03") == "2027-02-11"                  # Tết 2027
    assert factor.next_session_guess("2027-04-15") == "2027-04-19"                  # Hùng Kings on a Friday
    assert factor.next_session_guess("2026-10-14") == "2026-10-15"
    assert factor.next_session_guess("2026-10-16") == "2026-10-19"
    # Tết 2030 starts 31/1: 30/1 ends the month, and decide sees it on that day.
    assert factor.next_session_guess("2030-01-30") == "2030-02-07"
    book = Ledger(cash=1e9, start_cash=1e9)
    p = replace(qp.DEFAULT, fb_market_ma=10, fb_top_k=1)
    up = [100.0] * 9 + [200.0]
    factor.decide(book, "2030-01-29", "2030-01-30", up, p, _ranked("A"))
    d = factor.decide(book, "2030-01-30", factor.next_session_guess("2030-01-30"), up, p, _ranked("B"))
    assert d.rebalanced and d.checked and d.basket == ["B"]
    assert not any(vn_rules.is_session_day(day) for day in vn_rules.MARKET_HOLIDAYS)


@pytest.mark.unit
def test_ties_go_to_the_lower_volatility_and_the_market_filter_is_strict():
    def series(swing, last_vs_top):
        px = [100.0 * (1.001 ** i) * (1 + swing * (-1) ** i) for i in range(251)]
        return px + [max(px) * last_vs_top]

    calm, mid, wild = series(0.002, 0.90), series(0.01, 0.95), series(0.02, 1.01)
    ranked = factor.rank({"WILD": wild, "MID": mid, "CALM": calm}, ["WILD", "MID", "CALM"])
    # Low vol rank 3/2/1 + 52-week-high rank 1/2/3: all score 4 - the calmer first.
    assert {r.score for r in ranked} == {4.0}
    assert [r.symbol for r in ranked] == ["CALM", "MID", "WILD"]
    short = factor.rank({"NEW": [100.0] * 100}, ["NEW"])
    assert short == []                                              # under 150 returns: not scored
    assert factor.market_ok([100.0] * 200, 200) is False            # equal to the average is not above it
    assert factor.market_ok([100.0] * 199 + [101.0], 200) is True
    assert factor.market_ok([100.0] * 150, 200) is False            # not enough history


@pytest.mark.unit
def test_the_orders_follow_the_basket_and_size_on_nav():
    book = Ledger(cash=1e9, start_cash=1e9)
    p = replace(qp.DEFAULT, fb_top_k=10)
    decision = factor.Decision(True, True, ["AAA", "BBB"])
    orders = factor.plan(book, "2025-03-03", decision, {"AAA": 20_000.0}, p)    # BBB did not trade: no buy
    assert [(o.symbol, o.side) for o in orders] == [("AAA", "BUY")]
    assert orders[0].qty == vn_rules.round_lot(1e9 * 0.098 / (20_000 * 1.0015 * 1.02))
    assert factor.plan(book, "2025-03-03", decision, {"AAA": 20_000.0}, p) == []  # planned twice: no duplicate
    book.orders = []
    sells = factor.plan(book, "2025-03-04", factor.Decision(False, False, []), {}, p)
    assert sells == []                                              # nothing held yet


@pytest.mark.unit
def test_a_failed_universe_fails_the_session_instead_of_ranking_another(settings, monkeypatch, tmp_path):
    from tradingagents.dataflows import money_flows
    from tradingagents.dataflows.vendors import vci
    from vnbot.service import Watchlist
    from vnbot.trader import UNIVERSE_FILE, TradingBot

    def down():
        raise RuntimeError("listing down")

    monkeypatch.setattr(vci, "fetch_listing", down)
    monkeypatch.setattr(money_flows, "index_members", lambda group="VN30": ["VCB", "FPT"])
    bot = TradingBot(settings, Watchlist(settings))
    with pytest.raises(RuntimeError, match="thử lại"):                         # not today's VN30
        bot._factor_universe("2025-03-31", [], None, qp.DEFAULT)
    (bot.dir / UNIVERSE_FILE).write_text(
        '{"days": {"2025-03-10": {"size": 50, "universe": ["ACB", "MBB"], "pool": []}}}', encoding="utf-8")
    with pytest.raises(RuntimeError):                                          # nor an older build
        bot._factor_universe("2025-03-31", [], None, qp.DEFAULT)
    assert bot._factor_universe("2025-03-10", [], None, qp.DEFAULT) == ["ACB", "MBB"]   # that day's own build


@pytest.mark.unit
def test_build_universe_asks_a_failed_stock_again_and_stops_on_an_outage():
    sessions = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-01", periods=300)]
    symbols = [f"S{i:03d}" for i in range(40)]
    calls: dict[str, int] = {}

    def flaky(symbol):
        calls[symbol] = calls.get(symbol, 0) + 1
        if symbol in ("S005", "S006", "S007") and calls[symbol] == 1:
            raise RuntimeError("HTTP 504")                       # a passing error
        return {}

    built = factor.build_universe(sessions, symbols, 10, bars_of=lambda s: {}, history_of=flaky)
    assert built["universe"] == []                                # no data, but not refused: nothing missing
    assert calls["S005"] == 2 and calls["S000"] == 1             # asked again, once

    calls.clear()

    def outage(symbol):
        calls[symbol] = calls.get(symbol, 0) + 1
        raise RuntimeError("VCI down")

    with pytest.raises(factor.UniverseUnavailable, match="in a row"):
        factor.build_universe(sessions, symbols, 10, bars_of=lambda s: {}, history_of=outage)
    assert sum(calls.values()) == factor.MAX_FAILED_IN_A_ROW      # stopped: not 40 more requests


@pytest.mark.unit
def test_price_history_for_the_universe_does_not_trip_the_shared_breaker(monkeypatch, tmp_path):
    from tradingagents.dataflows import money_flows
    from tradingagents.dataflows.errors import VendorUnavailableError

    monkeypatch.setattr(money_flows, "_dir", lambda: str(tmp_path))
    money_flows.reset_breaker()
    asked = []

    def fetch(code, start, end):
        asked.append(code)
        if code in ("AAA", "CCC"):
            raise VendorUnavailableError("HTTP 504 from VCI")
        return pd.DataFrame({"Date": [pd.Timestamp("2026-10-14")], "MatchValue": [1e9], "Close": [20_000.0],
                             "TotalShares": [1e8]})

    monkeypatch.setattr(money_flows, "_vendor", lambda: SimpleNamespace(fetch_stock_flows=fetch))
    try:
        with pytest.raises(VendorUnavailableError):
            money_flows.load_price_history("AAA", "2026-10-14", guarded=False)
        frame = money_flows.load_price_history("BBB", "2026-10-14", guarded=False)    # still asked
        assert asked == ["AAA", "BBB"] and len(frame) == 1
        with pytest.raises(VendorUnavailableError):
            money_flows.load_price_history("CCC", "2026-10-14")                       # guarded: trips it
        with pytest.raises(VendorUnavailableError):
            money_flows.load_price_history("DDD", "2026-10-14")                       # ...and is shut out
        assert asked == ["AAA", "BBB", "CCC"]
    finally:
        money_flows.reset_breaker()


@pytest.mark.unit
def test_build_universe_refuses_when_the_sources_are_down():
    sessions = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-01", periods=300)]

    def history(symbol):
        raise RuntimeError("VCI down")

    with pytest.raises(factor.UniverseUnavailable):
        factor.build_universe(sessions, ["AAA", "BBB"], 50, bars_of=lambda s: {}, history_of=history)


@pytest.mark.unit
def test_old_books_keep_loading_and_the_defaults_follow_the_defensive_book(settings):
    import json

    from vnbot.service import Watchlist
    from vnbot.trader import TradingBot

    bot = TradingBot(settings, Watchlist(settings))
    old = {"cash": 5e8, "start_cash": 1e9, "positions": {}, "orders": [], "fills": [], "history": [
        {"date": "2026-10-13", "nav": 1e9, "benchmark": 1700}], "meta": {"mom_basket": ["FPT"], "mom_since": 3}}
    bot.momentum_path.write_text(json.dumps(old), encoding="utf-8")
    assert bot.momentum_book().meta["mom_basket"] == ["FPT"] and bot.momentum_book().cash == 5e8
    assert bot.factor_book().cash == settings.paper_capital and not bot.factor_book().positions
    assert settings.signal_strategy == "factor"
    assert set(qp.FACTOR_FIXED) <= set(qp.to_dict(qp.DEFAULT))
    # Set by hand: clamped to the range the evaluation tested.
    assert qp.coerce({"fb_top_k": 99, "fb_universe": 3}).fb_top_k == 15
    assert qp.coerce({"fb_universe": 3}).fb_universe == 30


@pytest.mark.unit
def test_the_defensive_book_is_never_calibrated(settings, monkeypatch):
    """Its parameters stay the evaluated ones: no strategy's calibration may move them."""
    from tradingagents.dataflows import money_flows
    from tradingagents.trading import calibrate as cal, simulator as sim
    from vnbot.service import Watchlist
    from vnbot.trader import TradingBot

    for keys in cal.STRATEGY_KEYS.values():
        assert not set(keys) & set(qp.FACTOR_FIXED)
    with pytest.raises(ValueError, match="not calibrated"):
        cal.calibrate(["AAA"], "2026-10-14", data=sim.Dataset(dates=[]), strategy="factor")

    ran = []

    def fake(symbols, day, *, apply=True, data=None, strategy="engine", champion=None):
        ran.append(strategy)
        return SimpleNamespace(summary=lambda: strategy)

    monkeypatch.setattr(cal, "calibrate", fake)
    monkeypatch.setattr(sim, "build_dataset", lambda *a, **k: sim.Dataset(dates=[]))
    monkeypatch.setattr(money_flows, "index_members", lambda group="VN30": ["FPT"])
    bot = TradingBot(settings, Watchlist(settings))
    bot.calibrate("2026-10-14")
    assert ran == ["engine", "momentum"]


def _book_with(history: list[tuple[str, float, float]]) -> Ledger:
    book = Ledger(cash=1e9, start_cash=1e9)
    book.start_date = history[0][0]
    book.history = [{"date": d, "nav": nav, "benchmark": bench} for d, nav, bench in history]
    book.cash = history[-1][1]
    return book


@pytest.mark.unit
def test_books_that_started_on_different_days_are_compared_over_the_same_period(monkeypatch):
    from vnbot import ui, web
    from vnbot.trader import TradingBot

    engine = _book_with([("2026-05-04", 1.0e9, 1200), ("2026-10-14", 0.95e9, 1700), ("2026-10-15", 0.96e9, 1717)])
    mom = _book_with([("2026-06-01", 1.0e9, 1300), ("2026-10-14", 1.1e9, 1700), ("2026-10-15", 1.1e9, 1717)])
    fb = _book_with([("2026-10-14", 1.0e9, 1700), ("2026-10-15", 1.0e9, 1717)])
    text = TradingBot._comparison(engine, mom, fb)
    assert "Phòng thủ +0.00% từ 2026-10-14" in text and "Quant Engine -4.00% từ 2026-05-04" in text
    assert "không cùng giai đoạn" in text
    assert "không cùng giai đoạn" not in TradingBot._comparison(fb, fb, fb)

    seen = {}
    monkeypatch.setattr(ui, "nav_chart", lambda dates, series, run_time=None: seen.update(dates=dates, series=series))
    trader = SimpleNamespace(paper=lambda: engine, momentum_book=lambda: mom, factor_book=lambda: fb,
                             settings=SimpleNamespace(trade_time="15:30"))
    web._chart(trader)
    at = seen["dates"].index("2026-10-14")
    lines = {s["name"]: s["values"] for s in seen["series"]}
    assert all(values[at] == 100 for values in lines.values())          # one common base for every line
    assert lines["Quant Engine"][0] == pytest.approx(1e9 / 0.95e9 * 100, abs=1e-3)   # history before it kept
    assert lines["VNINDEX"][-1] == pytest.approx(101.0)


@pytest.mark.unit
def test_a_change_of_signal_strategy_is_announced_once(settings):
    from vnbot.service import Watchlist
    from vnbot.trader import TradingBot

    bot = TradingBot(settings, Watchlist(settings))
    holding = SimpleNamespace(positions={"FPT": object()})
    empty = SimpleNamespace(positions={})
    assert bot._strategy_switch_notice({}, holding) == ""                       # a first report: nothing changed
    note = bot._strategy_switch_notice({"day": "2026-10-14", "text": "..."}, holding)  # written before the key
    assert "Động lượng" in note and "Phòng thủ" in note and "SIGNAL_STRATEGY=momentum" in note
    assert bot._strategy_switch_notice({"day": "2026-10-14"}, empty) == ""      # nothing held: nothing to sell
    assert bot._strategy_switch_notice({"day": "2026-10-15", "signal_strategy": "factor"}, holding) == ""
    assert "Quant Engine → Phòng thủ" in bot._strategy_switch_notice(
        {"day": "2026-10-15", "signal_strategy": "engine"}, holding)
    object.__setattr__(settings, "signal_strategy", "momentum")
    assert bot._strategy_switch_notice({"day": "2026-10-14"}, holding) == ""    # the old default kept
