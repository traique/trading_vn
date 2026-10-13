"""The quantitative engine end to end on synthetic bars: the system decision.

No network: prices, the listing, news and the sector are stubbed. The engine
must read everything as of the run date and hand the agents a plan that obeys
the exchange's tick size, price band and board lot.
"""

import math

import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.market import vn_rules
from tradingagents.portfolio import PortfolioContext
from tradingagents.quant import engine


def _bars(end: str, n: int = 260, start_price: float = 40_000.0, drift: float = 0.004) -> pd.DataFrame:
    days = pd.bdate_range(end=end, periods=n)
    closes = [start_price * math.exp(drift * i + 0.01 * math.sin(i / 3)) for i in range(n)]
    return pd.DataFrame({
        "Date": days,
        "Open": closes,
        "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes],
        "Close": closes,
        "Volume": [1_000_000 + (i % 7) * 50_000 for i in range(n)],
    })


@pytest.fixture
def market(monkeypatch):
    frames = {}

    def load(symbol, as_of, fill_gaps=True, sources=None):
        frame = frames.get(symbol)
        if frame is None:
            raise NoMarketDataError(symbol, symbol, "no rows")
        frame = frame[frame["Date"] <= pd.Timestamp(as_of)].copy()
        frame.attrs["source"] = "dnse"
        return frame

    monkeypatch.setattr(engine, "load_ohlcv", load)
    monkeypatch.setattr(engine, "exchange_of", lambda s: "HOSE")
    monkeypatch.setattr(engine, "_news_impact", lambda s, d: (0.0, []))
    monkeypatch.setattr(engine.sector, "sector_performance", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_live_quote_line", lambda *a: None)
    return frames


@pytest.mark.unit
def test_a_past_date_reads_only_its_own_bars_and_is_not_stale(market):
    market["FPT"] = _bars("2026-10-13")
    market["VNINDEX"] = _bars("2026-10-13", start_price=1_200.0, drift=0.001)

    result = engine.analyze("FPT", "2026-06-01")

    assert result.last_bar_date == "2026-06-01"
    assert result.data_quality == "ok"   # judged against the run date, not today
    assert result.action in engine.ACTION_LABEL_VI


@pytest.mark.unit
def test_every_level_is_a_valid_order_price_and_the_band_is_the_exchanges(market):
    market["FPT"] = _bars("2026-10-13")
    market["VNINDEX"] = _bars("2026-10-13", start_price=1_200.0, drift=0.001)

    r = engine.analyze("FPT", "2026-10-13")

    for level in (r.stop_price, r.target_price, r.entry_low, r.entry_high, r.target2):
        if level is not None:
            assert level % vn_rules.tick_size(level, "HOSE") == 0
    assert (r.ceiling, r.floor) == vn_rules.ceiling_floor(r.price, "HOSE")
    assert "±7% sàn HOSE" in r.report
    assert "ĐÃ CHỐT" in r.report and r.deterministic_summary.startswith("FPT")


@pytest.mark.unit
def test_a_buy_plan_respects_the_minimum_reward_and_board_lots(market, monkeypatch):
    market["FPT"] = _bars("2026-10-13")
    market["VNINDEX"] = _bars("2026-10-13", start_price=1_200.0, drift=0.001)
    book = PortfolioContext.model_validate({"cash": 500_000_000})

    r = engine.analyze("FPT", "2026-10-13", portfolio=book)

    if r.action == "BUY":
        assert r.rr_ratio >= 1.5
        assert r.entry_low > r.stop_price and r.target_price > r.entry_high
        assert r.suggested_shares and r.suggested_shares % vn_rules.LOT_SIZE == 0
    else:
        assert r.entry_low is None and r.suggested_shares is None


@pytest.mark.unit
def test_without_vnindex_the_regime_is_unknown_and_no_buy_is_issued(market):
    market["FPT"] = _bars("2026-10-13")

    r = engine.analyze("FPT", "2026-10-13")

    assert r.market_regime == "unknown"
    assert r.action != "BUY"


@pytest.mark.unit
def test_a_holding_is_read_from_the_book(market):
    market["FPT"] = _bars("2026-10-13", drift=-0.004)
    market["VNINDEX"] = _bars("2026-10-13", start_price=1_200.0, drift=0.001)
    book = PortfolioContext.model_validate({"positions": [{"ticker": "FPT.VN", "quantity": 1000, "average_price": 50_000}]})

    r = engine.analyze("FPT", "2026-10-13", portfolio=book)

    assert r.is_holding
    assert r.action != "NO_TRADE"          # a holder is told HOLD/WATCH/SELL, never "stay out"
    assert "Vị thế đang nắm giữ: 1.000 cp" in r.report


@pytest.mark.unit
def test_an_index_has_no_trading_decision(market):
    with pytest.raises(NoMarketDataError):
        engine.analyze("VNINDEX", "2026-10-13")


@pytest.mark.unit
def test_the_state_copy_round_trips_to_a_summary(market):
    market["FPT"] = _bars("2026-10-13")
    market["VNINDEX"] = _bars("2026-10-13", start_price=1_200.0, drift=0.001)
    state = engine.analyze("FPT", "2026-10-13").to_state()
    assert engine.render_summary_from_state(state).startswith("FPT")
    assert engine.render_summary_from_state({"action": "NO_TRADE", "reasons": ["x"]}).endswith("x")
