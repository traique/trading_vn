"""Money-flow history: incremental cache, point-in-time reads, failures leave the cache alone."""

import pandas as pd
import pytest

from tradingagents.dataflows import money_flows as mf
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.market import vn_rules


@pytest.fixture
def store(tmp_path, monkeypatch):
    from tradingagents.dataflows import config

    cfg = dict(config.get_config())
    cfg["data_cache_dir"] = str(tmp_path)
    monkeypatch.setattr(mf, "get_config", lambda: cfg)
    monkeypatch.setattr(mf, "published_through", lambda: pd.Timestamp("2026-10-13"))
    mf.reset_breaker()
    yield tmp_path
    mf.reset_breaker()


class _Vendor:
    def __init__(self):
        self.calls = []
        self.fail = False

    def _rows(self, start, end, value):
        days = pd.bdate_range(start, end)
        return days, value

    def fetch_stock_flows(self, code, start, end):
        self.calls.append(("foreign", code, start, end))
        if self.fail:
            raise VendorUnavailableError("down")
        days = pd.bdate_range(start, end)
        return pd.DataFrame({"Date": days, "ForeignBuy": 2e9, "ForeignSell": 1e9, "ForeignNet": 1e9,
                             "MatchValue": 50e9, "Close": 50_000.0, "TotalShares": 1e6, "ForeignRoomPct": 10.0})

    def fetch_proprietary_flows(self, code, start, end):
        self.calls.append(("prop", code, start, end))
        days = pd.bdate_range(start, end)
        return pd.DataFrame({"Date": days, "PropBuy": 1e9, "PropSell": 0.0, "PropNet": 1e9})

    def fetch_market_flows(self, start, end):
        self.calls.append(("market", start, end))
        days = pd.bdate_range(start, end)
        return pd.DataFrame({"Date": days, "ForeignNet": -1e11, "PropNet": 1e10})

    def fetch_group(self, group):
        if self.fail:
            raise VendorUnavailableError("down")
        return ["FPT", "HPG"]


@pytest.fixture
def vendor(monkeypatch):
    v = _Vendor()
    monkeypatch.setattr(mf, "_vendor", lambda: v)
    return v


@pytest.mark.unit
def test_a_series_is_downloaded_once_then_read_point_in_time(store, vendor):
    frame = mf.load_stock_flows("FPT", "2026-10-13")
    assert not frame.empty and frame["Date"].max() == pd.Timestamp("2026-10-13")
    assert "PropNet" in frame and frame["PropNet"].iloc[-1] == 1e9
    calls = len(vendor.calls)
    past = mf.load_stock_flows("FPT", "2026-09-30")
    assert past["Date"].max() <= pd.Timestamp("2026-09-30")
    assert len(vendor.calls) == calls                       # served from the cache


@pytest.mark.unit
def test_backfill_reaches_further_back_than_an_analysis(store, vendor):
    recent = mf.load_market_flows("2026-10-13")
    assert recent["Date"].min() >= pd.Timestamp("2026-10-13") - pd.Timedelta(days=mf.RECENT_DAYS + 3)
    full = mf.load_market_flows("2026-10-13", backfill=True)
    assert full["Date"].min() < recent["Date"].min()


@pytest.mark.unit
def test_a_failed_download_keeps_the_cache_and_opens_the_breaker(store, vendor):
    mf.load_stock_flows("FPT", "2026-10-13")
    vendor.fail = True
    cached = mf.load_stock_flows("HPG", "2026-10-13")       # nothing cached for HPG
    assert cached.empty
    calls = len(vendor.calls)
    mf.load_stock_flows("VCB", "2026-10-13")
    assert len(vendor.calls) == calls                       # host just failed: not asked again


@pytest.mark.unit
def test_basket_members_fall_back_to_the_last_known_list(store, vendor):
    assert mf.index_members("VN30") == ["FPT", "HPG"]
    vendor.fail = True
    mf.reset_breaker()
    assert mf.index_members("VN30") == ["FPT", "HPG"]       # today's cached list
    assert len(mf.VN30_FALLBACK) == 30


@pytest.mark.unit
def test_flows_are_published_after_the_close(monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(vn_rules, "now_vn", lambda: datetime(2026, 10, 14, 10, 0, tzinfo=vn_rules.VN_TZ))
    assert mf.published_through() == pd.Timestamp("2026-10-13")
    monkeypatch.setattr(vn_rules, "now_vn", lambda: datetime(2026, 10, 14, 16, 0, tzinfo=vn_rules.VN_TZ))
    assert mf.published_through() == pd.Timestamp("2026-10-14")
