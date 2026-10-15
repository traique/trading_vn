"""Regression tests for the price cache, the price-source breaker, VCI index codes,
market-flow refills and the breadth adjustment basis."""

import json
import os
import time
from datetime import datetime

import pandas as pd
import pytest

from tradingagents.dataflows import money_flows as mf, ohlcv
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import http, vci
from tradingagents.market import vn_rules


def _vn(*args) -> datetime:
    return datetime(*args, tzinfo=vn_rules.VN_TZ)


def _bars(days, close=56000.0):
    return pd.DataFrame({"Date": days, "Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": 1000})


def _touch(path, at: datetime) -> None:
    stamp = at.timestamp()
    os.utime(path, (stamp, stamp))


@pytest.fixture
def prices(monkeypatch, tmp_path):
    """The price loader with stable fake sources; returns (sources, calls)."""
    sources: dict = {}
    calls: dict = {"dnse": 0, "vci": 0}

    def fetcher(name):
        def fetch(symbol, start, end):
            calls[name] += 1
            value = sources.get(name)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise NoMarketDataError(symbol, symbol, "no rows")
            return value
        return fetch

    fetchers = {"dnse": fetcher("dnse"), "vci": fetcher("vci")}
    config = {"data_cache_dir": str(tmp_path), "data_vendors": {"core_stock_apis": "dnse,vci"}}
    monkeypatch.setattr(ohlcv, "get_config", lambda: config)
    monkeypatch.setattr(ohlcv, "_fetchers", lambda: fetchers)
    ohlcv.reset_breaker()
    yield sources, calls
    ohlcv.reset_breaker()


@pytest.fixture
def utc_host():
    """The host clock in UTC, as in the Docker image (no TZ set)."""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


# ─── Cache freshness in Vietnam time ─────────────────────────────────────────


@pytest.mark.unit
def test_a_forming_bar_cached_yesterday_is_refetched_before_7am_on_a_utc_host(prices, utc_host, monkeypatch):
    sources, calls = prices
    path = ohlcv._cache_file("FPT", "dnse")
    # Cached at 10:00 VN on 15/10 (03:00 UTC) with the forming bar of 15/10.
    _bars(["2026-10-14", "2026-10-15"], close=56000.0).to_csv(path, index=False)
    _touch(path, _vn(2026, 10, 15, 10, 0))
    # 06:00 VN on 16/10 is still 15/10 in UTC; a catch-up run asks for the 15/10 session.
    monkeypatch.setattr(vn_rules, "now_vn", lambda: _vn(2026, 10, 16, 6, 0))
    sources["dnse"] = _bars(["2026-10-14", "2026-10-15"], close=57000.0)

    frame = ohlcv.load_ohlcv("FPT", "2026-10-15")

    assert calls["dnse"] == 1
    assert frame["Close"].iloc[-1] == 57000.0


@pytest.mark.unit
def test_cache_freshness_is_judged_on_the_vietnam_day_and_the_final_bar(prices, utc_host, monkeypatch):
    path = ohlcv._cache_file("FPT", "dnse")
    _bars(["2026-10-15"]).to_csv(path, index=False)
    today = pd.Timestamp("2026-10-15")

    def fresh(written, now, as_of):
        _touch(path, written)
        monkeypatch.setattr(vn_rules, "now_vn", lambda: now)
        return ohlcv._cache_is_fresh(path, pd.Timestamp(as_of), pd.Timestamp(now.date()))

    # Written on another VN day: stale, whatever the UTC dates say.
    assert not fresh(_vn(2026, 10, 15, 10, 0), _vn(2026, 10, 16, 6, 0), "2026-10-15")
    # Written today (VN) for a past session: fresh.
    assert fresh(_vn(2026, 10, 16, 1, 0), _vn(2026, 10, 16, 6, 0), "2026-10-15")
    # A run dated today: within the TTL...
    assert fresh(_vn(2026, 10, 15, 15, 10), _vn(2026, 10, 15, 15, 20), today)
    # ...but a download from before the bar was final is not reused after the close.
    assert not fresh(_vn(2026, 10, 15, 14, 55), _vn(2026, 10, 15, 15, 6), today)
    # Past the TTL: stale.
    assert not fresh(_vn(2026, 10, 15, 15, 10), _vn(2026, 10, 15, 15, 40), today)


# ─── Price-source breaker ────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_source_that_is_down_is_not_asked_again_on_every_load(prices, monkeypatch):
    sources, calls = prices
    monkeypatch.setattr(vn_rules, "now_vn", lambda: _vn(2026, 10, 15, 16, 0))
    sources["dnse"] = VendorUnavailableError("DNSE timed out")
    sources["vci"] = _bars(["2026-10-14", "2026-10-15"])

    assert ohlcv.load_ohlcv("FPT", "2026-10-15").attrs["source"] == "vci"
    assert ohlcv.load_ohlcv("HPG", "2026-10-15").attrs["source"] == "vci"
    assert calls["dnse"] == 1                       # the breaker skipped DNSE the second time

    monkeypatch.setattr(ohlcv.time, "time", lambda: time.time_ns() / 1e9 + ohlcv.BREAKER_SECONDS + 1)
    sources["dnse"] = _bars(["2026-10-14", "2026-10-15"])
    assert ohlcv.load_ohlcv("VCB", "2026-10-15").attrs["source"] == "dnse"
    assert calls["dnse"] == 2                       # asked again once the breaker closed


@pytest.mark.unit
def test_no_data_does_not_open_the_breaker(prices, monkeypatch):
    sources, calls = prices
    monkeypatch.setattr(vn_rules, "now_vn", lambda: _vn(2026, 10, 15, 16, 0))
    sources["vci"] = _bars(["2026-10-15"])
    ohlcv.load_ohlcv("ZZZ", "2026-10-15")
    ohlcv.load_ohlcv("YYY", "2026-10-15")
    assert calls["dnse"] == 2


@pytest.mark.unit
def test_an_older_file_still_serves_a_final_session_when_its_source_is_down(prices, monkeypatch):
    sources, calls = prices
    path = ohlcv._cache_file("FPT", "dnse")
    _bars(["2026-10-13", "2026-10-14"], close=55000.0).to_csv(path, index=False)
    _touch(path, _vn(2026, 10, 14, 16, 0))          # after the 14/10 close
    monkeypatch.setattr(vn_rules, "now_vn", lambda: _vn(2026, 10, 15, 10, 0))
    sources["dnse"] = VendorUnavailableError("down")
    sources["vci"] = _bars(["2026-10-14"], close=1.0)

    frame = ohlcv.load_ohlcv("FPT", "2026-10-14")
    assert frame.attrs["source"] == "dnse" and frame["Close"].iloc[-1] == 55000.0
    assert calls["vci"] == 0

    # The same file cannot serve a session it was written before.
    with pytest.raises(VendorUnavailableError):
        ohlcv._read_source("FPT", "dnse", pd.Timestamp("2026-10-15"))


# ─── Unreadable cache ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_an_empty_cache_file_is_downloaded_again(prices, monkeypatch):
    path = ohlcv._cache_file("FPT", "dnse")
    open(path, "w").close()                          # zero bytes, written now
    downloads = []

    def download(canonical, source):
        downloads.append(source)
        return _bars(["2026-10-12", "2026-10-13"])

    monkeypatch.setattr(ohlcv, "_download", download)
    frame = ohlcv.load_ohlcv("FPT", "2026-10-13")
    assert downloads == ["dnse"] and len(frame) == 2


@pytest.mark.unit
def test_a_garbled_cache_file_is_downloaded_again(prices, monkeypatch):
    path = ohlcv._cache_file("FPT", "dnse")
    with open(path, "wb") as handle:
        handle.write(b'"Date,Open\n\x00\x00"unterminated')
    monkeypatch.setattr(ohlcv, "_download", lambda canonical, source: _bars(["2026-10-13"]))
    assert len(ohlcv.load_ohlcv("FPT", "2026-10-13")) == 1


# ─── VCI index codes ─────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("symbol, code", [
    ("HNX", "HNXIndex"), ("UPCOM", "HNXUpcomIndex"), ("VNINDEX", "VNINDEX"), ("VN30", "VN30"),
    ("HNX30", "HNX30"), ("FPT", "FPT"),
])
def test_vci_is_asked_for_indices_by_its_own_codes(monkeypatch, symbol, code):
    sent = []
    day = int(pd.Timestamp("2026-10-15").timestamp())

    def post_json(url, **kwargs):
        sent.append(kwargs["json"]["symbols"])
        return [{"t": [day], "o": [265.0], "h": [266.0], "l": [264.0], "c": [265.16], "v": [1000]}]

    monkeypatch.setattr(http, "post_json", post_json)
    frame = vci.fetch_ohlcv(symbol, "2026-10-10", "2026-10-15")
    assert sent == [[code]]
    assert len(frame) == 1


# ─── Market flows with one side missing ──────────────────────────────────────


@pytest.fixture
def flows(tmp_path, monkeypatch):
    from tradingagents.dataflows import config

    cfg = dict(config.get_config())
    cfg["data_cache_dir"] = str(tmp_path)
    monkeypatch.setattr(mf, "get_config", lambda: cfg)
    monkeypatch.setattr(mf, "published_through", lambda: pd.Timestamp("2026-10-15"))
    mf.reset_breaker()
    yield tmp_path
    mf.reset_breaker()


class _MarketVendor:
    def __init__(self):
        self.calls = []
        self.prop_through = pd.Timestamp("2026-10-14")   # tự doanh not yet out for 15/10

    def fetch_market_flows(self, start, end):
        self.calls.append((start, end))
        days = pd.bdate_range(start, end)
        prop = [1e10 if d <= self.prop_through else float("nan") for d in days]
        return pd.DataFrame({"Date": days, "ForeignNet": -1e11, "PropNet": prop})


def _expire_recheck(path):
    with open(f"{path}.json", encoding="utf-8") as handle:
        meta = json.load(handle)
    meta["asked_at"] = 0
    with open(f"{path}.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle)


@pytest.mark.unit
def test_a_day_saved_with_one_side_missing_is_fetched_again(flows, monkeypatch):
    vendor = _MarketVendor()
    monkeypatch.setattr(mf, "_vendor", lambda: vendor)
    path = os.path.join(mf._dir(), "market-flows.csv")

    first = mf.load_market_flows("2026-10-15")
    assert pd.isna(first["PropNet"].iloc[-1])

    mf.load_market_flows("2026-10-15")
    assert len(vendor.calls) == 1                   # rechecks stay throttled

    _expire_recheck(path)
    vendor.prop_through = pd.Timestamp("2026-10-15")
    filled = mf.load_market_flows("2026-10-15")
    assert vendor.calls[-1] == ("2026-10-15", "2026-10-15")
    assert filled["PropNet"].iloc[-1] == 1e10


@pytest.mark.unit
def test_a_refetch_missing_a_value_does_not_erase_the_cached_one(flows, monkeypatch):
    vendor = _MarketVendor()
    vendor.prop_through = pd.Timestamp("2026-10-15")
    monkeypatch.setattr(mf, "_vendor", lambda: vendor)
    path = os.path.join(mf._dir(), "market-flows.csv")
    mf.load_market_flows("2026-10-15")
    # The foreign side of 15/10 goes missing in the cache, and the vendor then drops tự doanh.
    cached = pd.read_csv(path)
    cached.loc[cached.index[-1], "ForeignNet"] = float("nan")
    cached.to_csv(path, index=False)
    _expire_recheck(path)
    vendor.prop_through = pd.Timestamp("2026-10-14")

    frame = mf.load_market_flows("2026-10-15")
    assert frame["ForeignNet"].iloc[-1] == -1e11
    assert frame["PropNet"].iloc[-1] == 1e10


@pytest.mark.unit
def test_old_holes_are_not_asked_for_again(flows, monkeypatch):
    vendor = _MarketVendor()
    vendor.prop_through = pd.Timestamp("2026-09-30")
    monkeypatch.setattr(mf, "_vendor", lambda: vendor)
    path = os.path.join(mf._dir(), "market-flows.csv")
    mf.load_market_flows("2026-10-15")
    _expire_recheck(path)
    mf.load_market_flows("2026-10-15")
    start = pd.Timestamp(vendor.calls[-1][0])
    assert start >= pd.Timestamp("2026-10-15") - pd.Timedelta(days=mf.REFILL_DAYS)


# ─── Breadth on one adjustment basis ─────────────────────────────────────────


def _hose(code_rows):
    return pd.DataFrame([{"Code": c, "Close": close, "AdClose": ad, "Change": change,
                          "Ceiling": 0.0, "Floor": 0.0} for c, close, ad, change in code_rows])


@pytest.mark.unit
def test_an_ex_dividend_day_rescales_the_cached_closes(flows, monkeypatch):
    sessions = list(pd.bdate_range(end="2026-10-15", periods=56))
    old, ex_day = sessions[:-1], sessions[-1]
    # 55 sessions cached by daily top-ups, each at the basis of its own day.
    rows = [{"Date": d, "Advances": 0, "Declines": 0, "Unchanged": 2, "Ceilings": 0, "Floors": 0,
             "Total": 2} for d in old]
    closes = [pd.Series([100.0, 50.0], index=["AAA", "BBB"], name=d) for d in old]
    mf._merge_breadth(rows, closes)

    class Vendor:
        def fetch_hose_day(self, day):
            assert pd.Timestamp(day) == ex_day
            # AAA goes ex a 15% dividend (reference 85) and closes up 1; BBB is unchanged.
            return _hose([("AAA", 86.0, 86.0, 1.0), ("BBB", 50.0, 50.0, 0.0)])

    monkeypatch.setattr(mf, "_vendor", lambda: Vendor())
    monkeypatch.setattr(mf, "_sessions", lambda start, end: [d for d in sessions if start <= d <= end])
    breadth = mf.load_breadth("2026-10-15")

    matrix = pd.read_csv(mf._breadth_paths()[1], index_col="Date")
    assert matrix["AAA"].iloc[:-1].round(6).eq(85.0).all()
    assert matrix["AAA"].iloc[-1] == 86.0
    assert matrix["BBB"].eq(50.0).all()
    last = breadth.iloc[-1]
    assert last["PctAboveMA50"] == 50.0             # AAA is above its (adjusted) average
    assert last["NewHighs20"] == 2                  # AAA at a high, BBB flat at its high


@pytest.mark.unit
def test_breadth_history_is_left_alone_without_the_previous_session(flows):
    sessions = list(pd.bdate_range(end="2026-10-15", periods=4))
    rows = [{"Date": sessions[0], "Total": 1}]
    mf._merge_breadth(rows, [pd.Series([100.0], index=["AAA"], name=sessions[0])])
    # The session before the new one (sessions[2]) is not cached: the basis cannot be told.
    mf._merge_breadth([{"Date": sessions[3], "Total": 1}],
                      [pd.Series([86.0], index=["AAA"], name=sessions[3])],
                      [pd.Series([85.0], index=["AAA"], name=sessions[3])], sessions)
    matrix = pd.read_csv(mf._breadth_paths()[1], index_col="Date")
    assert list(matrix["AAA"]) == [100.0, 86.0]


@pytest.mark.unit
def test_backfilled_sessions_on_todays_basis_are_not_rescaled(flows):
    sessions = list(pd.bdate_range(end="2026-10-15", periods=4))
    s0, s1, s2, s3 = sessions
    mf._merge_breadth([{"Date": s2, "Total": 1}], [pd.Series([100.0], index=["AAA"], name=s2)])
    # A backfill fetched today: s3 went ex (reference 85 vs 100 cached for s2); s0 and s1
    # come already adjusted by VNDirect.
    mf._merge_breadth(
        [{"Date": d, "Total": 1} for d in (s3, s1, s0)],
        [pd.Series([v], index=["AAA"], name=d) for d, v in ((s3, 85.0), (s1, 85.0), (s0, 85.0))],
        [pd.Series([r], index=["AAA"], name=d) for d, r in ((s3, 85.0), (s1, 85.0), (s0, 85.0))],
        sessions,
    )
    matrix = pd.read_csv(mf._breadth_paths()[1], index_col="Date")
    assert list(matrix["AAA"].round(6)) == [85.0, 85.0, 85.0, 85.0]
