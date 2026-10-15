"""The Vietnamese price path: DNSE/VCI parsing, the point-in-time loader, symbols.

No network: vendor HTTP calls are replaced by canned responses shaped like the
live ones (verified against DNSE and VCI on 13/10/2026).
"""

from datetime import datetime

import pandas as pd
import pytest

from tradingagents.dataflows import ohlcv
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.router import no_data_available, route_to_vendor
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors import dnse, vci
from tradingagents.market import vn_rules


def _epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=9, tzinfo=vn_rules.VN_TZ).timestamp())


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.ok = status < 400

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


# ─── Symbols ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("raw, canonical", [
    ("fpt", "FPT"), ("FPT.VN", "FPT"), ("HOSE:FPT", "FPT"), ("hnx:shs", "SHS"), ("SSI.HM", "SSI"),
    ("VN-Index", "VNINDEX"), ("^VNINDEX", "VNINDEX"), ("vni", "VNINDEX"),
    ("HNXINDEX", "HNX"), ("UPCOMINDEX", "UPCOM"), ("VN30", "VN30"),
])
def test_symbols_resolve_to_the_vietnamese_code(raw, canonical):
    assert normalize_symbol(raw) == canonical


@pytest.mark.unit
def test_a_path_escaping_ticker_is_refused():
    with pytest.raises(ValueError):
        safe_ticker_component("../../etc")
    assert safe_ticker_component("FPT") == "FPT"


@pytest.mark.unit
def test_no_data_sentinel_names_the_symbol():
    text = no_data_available(NoMarketDataError("fpt.vn", "FPT", "no rows"))
    assert text.startswith("NO_DATA_AVAILABLE") and "FPT" in text and "fabricate" in text


# ─── DNSE ────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_dnse_stock_prices_are_scaled_to_vnd(monkeypatch):
    payload = {"t": [_epoch("2026-10-12"), _epoch("2026-10-13")], "o": [58.1, 57.1],
               "h": [58.8, 57.4], "l": [57.1, 56.1], "c": [57.1, 56.3], "v": [5930000, 6775400]}
    calls = []
    monkeypatch.setattr(dnse.http, "request", lambda *a, **k: calls.append(k) or _Response(200, payload))

    frame = dnse.fetch_ohlcv("FPT", "2026-10-12", "2026-10-13")

    assert calls[0]["params"]["symbol"] == "FPT"
    assert list(frame["Date"]) == ["2026-10-12", "2026-10-13"]
    assert list(frame["Close"]) == [57100, 56300]
    assert list(frame["Open"]) == [58100, 57100]
    assert list(frame["Volume"]) == [5930000, 6775400]


@pytest.mark.unit
def test_dnse_index_keeps_points_and_decimals(monkeypatch):
    payload = {"t": [_epoch("2026-10-13")], "o": [1716.87], "h": [1726.35], "l": [1710.95],
               "c": [1719.42], "v": [556951040]}
    seen = {}
    monkeypatch.setattr(dnse.http, "request", lambda method, url, **k: seen.update(url=url, **k) or _Response(200, payload))

    frame = dnse.fetch_ohlcv("VNINDEX", "2026-10-13", "2026-10-13")

    assert seen["url"].endswith("/index")
    assert frame["Close"].iloc[0] == pytest.approx(1719.42)


@pytest.mark.unit
def test_dnse_unknown_symbol_is_no_data_not_an_outage(monkeypatch):
    monkeypatch.setattr(dnse.http, "request", lambda *a, **k: _Response(400, {"message": "invalid symbol"}))
    with pytest.raises(NoMarketDataError):
        dnse.fetch_ohlcv("ZZZZ", "2026-10-01", "2026-10-13")


@pytest.mark.unit
def test_dnse_list_body_is_no_data(monkeypatch):
    monkeypatch.setattr(dnse.http, "request", lambda *a, **k: _Response(200, []))
    with pytest.raises(NoMarketDataError):
        dnse.fetch_ohlcv("FPT", "2026-10-01", "2026-10-13")


@pytest.mark.unit
def test_a_malformed_dnse_bar_is_dropped_alone():
    frame = dnse.bars_to_frame(
        {"t": [_epoch("2026-10-12"), "x", _epoch("2026-10-13")], "c": [57.1, 56.0, 0],
         "o": [], "h": [], "l": [], "v": []},
        scale=1000, index=False,
    )
    assert list(frame["Close"]) == [57100]


# ─── VCI prices ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_vci_prices_are_already_vnd_and_windowed(monkeypatch):
    bars = [{"symbol": "FPT", "t": ["1791763200", "1791849600"], "o": [58100, 57100], "h": [58800, 57400],
             "l": [57100, 56100], "c": [57100, 56300], "v": [5930000, 6775400]}]
    monkeypatch.setattr(vci.http, "post_json", lambda *a, **k: bars)

    frame = vci.fetch_ohlcv("FPT", "2026-10-13", "2026-10-13")

    assert list(frame["Date"]) == ["2026-10-13"]
    assert list(frame["Close"]) == [56300]


@pytest.mark.unit
def test_vci_empty_answer_is_no_data(monkeypatch):
    monkeypatch.setattr(vci.http, "post_json", lambda *a, **k: [])
    with pytest.raises(NoMarketDataError):
        vci.fetch_ohlcv("FPT", "2026-10-01", "2026-10-13")


# ─── The point-in-time loader ────────────────────────────────────────────────


def _frame(days, close=10000.0):
    return pd.DataFrame({"Date": days, "Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": 1000})


@pytest.fixture
def loader(monkeypatch, tmp_path):
    """The loader with canned sources; returns the dict of sources to fill."""
    sources = {}

    def fetcher(name):
        def fetch(symbol, start, end):
            value = sources.get(name)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise NoMarketDataError(symbol, symbol, "no rows")
            return value
        return fetch

    config = {"data_cache_dir": str(tmp_path), "data_vendors": {"core_stock_apis": "dnse,vci"}}
    monkeypatch.setattr(ohlcv, "get_config", lambda: config)
    monkeypatch.setattr(ohlcv, "_fetchers", lambda: {"dnse": fetcher("dnse"), "vci": fetcher("vci")})
    monkeypatch.setattr(ohlcv, "_today", lambda: pd.Timestamp("2026-10-13"))
    return sources


@pytest.mark.unit
def test_rows_after_the_run_date_are_never_served(loader):
    loader["dnse"] = _frame(["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"])
    frame = ohlcv.load_ohlcv("FPT", "2026-10-09")
    assert frame["Date"].max() == pd.Timestamp("2026-10-09")


@pytest.mark.unit
def test_vci_serves_when_dnse_has_nothing(loader):
    loader["dnse"] = NoMarketDataError("FPT", "FPT", "no rows")
    loader["vci"] = _frame(["2026-10-12", "2026-10-13"])
    frame = ohlcv.load_ohlcv("FPT", "2026-10-13")
    assert frame.attrs["source"] == "vci" and len(frame) == 2


@pytest.mark.unit
def test_vci_serves_when_dnse_is_down(loader):
    loader["dnse"] = VendorUnavailableError("DNSE request failed")
    loader["vci"] = _frame(["2026-10-13"])
    assert ohlcv.load_ohlcv("FPT", "2026-10-13").attrs["source"] == "vci"


@pytest.mark.unit
def test_every_source_down_is_unavailable_not_no_data(loader):
    loader["dnse"] = VendorUnavailableError("down")
    loader["vci"] = VendorUnavailableError("down")
    with pytest.raises(VendorUnavailableError):
        ohlcv.load_ohlcv("FPT", "2026-10-13")


@pytest.mark.unit
def test_a_stale_frame_is_refused(loader):
    loader["dnse"] = _frame(["2026-08-01", "2026-08-03"])
    loader["vci"] = _frame(["2026-08-01"])
    with pytest.raises(NoMarketDataError, match="stale"):
        ohlcv.load_ohlcv("FPT", "2026-10-13")


@pytest.mark.unit
def test_an_empty_download_is_not_cached(loader, tmp_path):
    loader["vci"] = _frame(["2026-10-13"])
    ohlcv.load_ohlcv("FPT", "2026-10-13")
    assert not (tmp_path / "FPT-dnse-ohlcv.csv").exists()
    assert (tmp_path / "FPT-vci-ohlcv.csv").exists()


@pytest.mark.unit
def test_todays_forming_bar_is_dropped_during_the_session():
    frame = pd.DataFrame({"Date": pd.to_datetime(["2026-10-12", "2026-10-13"]), "Close": [1.0, 2.0]})
    during = datetime(2026, 10, 13, 10, 30, tzinfo=vn_rules.VN_TZ)
    after = datetime(2026, 10, 13, 15, 30, tzinfo=vn_rules.VN_TZ)
    as_of = pd.Timestamp("2026-10-13")
    assert len(ohlcv.drop_forming_bar(frame, as_of, now=during)) == 1
    assert len(ohlcv.drop_forming_bar(frame, as_of, now=after)) == 2


@pytest.mark.unit
def test_todays_bar_is_not_final_between_the_put_through_end_and_15_05():
    # Regression: 15:00-15:04 served a forming (e.g. 14:50) snapshot as the close,
    # while the cache rules only treat a bar as final from 15:05.
    frame = pd.DataFrame({"Date": pd.to_datetime(["2026-10-12", "2026-10-13"]), "Close": [1.0, 2.0]})
    as_of = pd.Timestamp("2026-10-13")
    at_1502 = datetime(2026, 10, 13, 15, 2, tzinfo=vn_rules.VN_TZ)
    at_1505 = datetime(2026, 10, 13, 15, 5, tzinfo=vn_rules.VN_TZ)
    assert len(ohlcv.drop_forming_bar(frame, as_of, now=at_1502)) == 1
    assert len(ohlcv.drop_forming_bar(frame, as_of, now=at_1505)) == 2
    assert len(ohlcv.drop_forming_bar(frame, pd.Timestamp("2026-10-12"), now=at_1502)) == 2   # a past day


@pytest.mark.unit
def test_stock_data_text_states_vnd_and_the_source(loader):
    loader["dnse"] = _frame(["2026-10-12", "2026-10-13"], close=56300.0)
    text = ohlcv.get_stock_data("fpt.vn", "2026-10-12", "2026-10-13", source="dnse")
    assert "FPT" in text and "VND" in text and "DNSE" in text
    assert "2026-10-13,56300,56300,56300,56300,1000" in text


@pytest.mark.unit
def test_the_router_reports_no_data_for_an_unknown_code(loader):
    out = route_to_vendor("get_stock_data", "ZZZZ", "2026-10-01", "2026-10-13")
    assert out.startswith("NO_DATA_AVAILABLE")


@pytest.mark.unit
def test_closes_for_settlement_exclude_the_end_day(loader):
    loader["dnse"] = _frame(["2026-10-08", "2026-10-09", "2026-10-12"])
    closes = ohlcv.get_closes("FPT", "2026-10-09", "2026-10-12")
    assert list(closes.index) == [pd.Timestamp("2026-10-09")]


# ─── Market rules ────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("price, exchange, tick", [
    (9_990, "HOSE", 10), (10_000, "HOSE", 50), (49_950, "HSX", 50), (50_000, "HOSE", 100),
    (9_990, "HNX", 100), (60_000, "UPCOM", 100), (9_990, None, 100),
])
def test_tick_size_by_exchange(price, exchange, tick):
    assert vn_rules.tick_size(price, exchange) == tick


@pytest.mark.unit
def test_ceiling_and_floor_follow_each_exchange_band():
    assert vn_rules.ceiling_floor(56_300, "HOSE") == (60_200, 52_400)
    assert vn_rules.ceiling_floor(11_700, "HNX") == (12_800, 10_600)
    assert vn_rules.ceiling_floor(40_700, "UPCOM") == (46_800, 34_600)
    ceiling, floor = vn_rules.ceiling_floor(56_300, "HOSE")
    assert ceiling <= 56_300 * 1.07 and floor >= 56_300 * 0.93


@pytest.mark.unit
def test_shares_are_whole_board_lots():
    assert vn_rules.round_lot(1_299) == 1_200
    assert vn_rules.shares_for(100_000_000, 10, 56_300) == 100
    assert vn_rules.shares_for(0, 10, 56_300) == 0


@pytest.mark.unit
@pytest.mark.parametrize("hhmm, phase", [
    ("08:59", "pre_open"), ("09:05", "ato"), ("10:00", "continuous"), ("12:00", "lunch"),
    ("14:35", "atc"), ("14:50", "put_through"), ("15:30", "after_close"),
])
def test_session_phases(hhmm, phase):
    hour, minute = map(int, hhmm.split(":"))
    assert vn_rules.session_phase(datetime(2026, 10, 13, hour, minute, tzinfo=vn_rules.VN_TZ)) == phase


@pytest.mark.unit
def test_weekends_are_closed():
    assert vn_rules.session_phase(datetime(2026, 10, 11, 10, 0, tzinfo=vn_rules.VN_TZ)) == "closed"
    assert not vn_rules.is_market_hours(datetime(2026, 10, 11, 10, 0, tzinfo=vn_rules.VN_TZ))
