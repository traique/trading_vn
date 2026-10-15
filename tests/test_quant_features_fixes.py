"""Regression tests for the quant feature fixes: UPCoM floor days vs bonus gaps,
matched-only foreign flow, value-based liquidity, Tết staleness, sector alignment."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.quant import (
    features as feat,
    flow_features as ff,
    price_adjust,
    sector,
    validation as val,
)

VN = ZoneInfo("Asia/Ho_Chi_Minh")


# ─── price_adjust: a UPCoM floor day is not a 1:6 bonus gap ──────────────────


@pytest.mark.unit
@pytest.mark.parametrize("move", [-12.9, -13.5, -14.0, -15.0])
def test_upcom_floor_day_is_not_a_gap(move):
    closes = [10_000, round(10_000 * (1 + move / 100))]
    assert price_adjust.detect_price_gaps(closes, exchange="UPCOM") == []


@pytest.mark.unit
def test_real_tvn_floor_day_is_not_flagged_and_does_not_block_the_window():
    gaps = price_adjust.detect_price_gaps([8990, 7640], exchange="UPCOM")  # -15,02%
    assert gaps == []
    assert price_adjust.last_gap_in_window(gaps, 60) is None


@pytest.mark.unit
def test_bonus_sized_drop_outside_the_band_is_still_flagged():
    for exchange in ("HNX", "HOSE"):
        gaps = price_adjust.detect_price_gaps([14_000, 12_000], exchange=exchange)  # -14,29%
        assert gaps and gaps[0].ratio_hint == "thưởng tỷ lệ 1:6"
    assert price_adjust.detect_price_gaps([12_000, 10_000], exchange="UPCOM")      # 1:5, beyond ±15%
    # Unknown exchange: unchanged (resolution belongs to the caller).
    assert price_adjust.detect_price_gaps([14_000, 12_000])


# ─── flow_features: negotiated deals do not drive the foreign signal ─────────


def _flow_frame(n=20, *, total=None, matched=None, match_value=4e11):
    dates = pd.bdate_range(end="2026-10-14", periods=n)
    frame = pd.DataFrame({
        "Date": dates,
        "ForeignNet": [total[i] if total else 1e9 for i in range(n)],
        "MatchValue": [match_value] * n,
    })
    if matched is not None:
        frame["ForeignNetMatched"] = matched
    return frame


@pytest.mark.unit
def test_block_deal_does_not_flip_the_foreign_ratio():
    total = [1e9] * 20
    total[-3] = -9.8e12                                  # VIC 2025-08-04-style deal
    matched = [1e9] * 20
    matched[-3] = 2e9
    out = ff.stock_flows(_flow_frame(total=total, matched=matched), "2026-10-14")
    p_min = 3.0  # Params.foreign_ratio_min default
    assert out.foreign_ratio_20 > -p_min
    assert out.foreign_ratio_20 == pytest.approx(21e9 / 8e12 * 100, abs=0.01)
    assert out.foreign_streak == 20                      # the deal day does not break the buy streak
    assert out.foreign_sell_days_10 == 0
    assert out.foreign_net_20 == pytest.approx(21e9)
    assert out.foreign_net_total_20 == pytest.approx(19e9 - 9.8e12)


@pytest.mark.unit
def test_cached_rows_without_matched_split_fall_back_to_total():
    total = [-1e9] * 20
    matched = [float("nan")] * 15 + [1e9] * 5           # older cache rows lack the column
    out = ff.stock_flows(_flow_frame(total=total, matched=matched), "2026-10-14")
    assert out.foreign_net_20 == pytest.approx(-15e9 + 5e9)
    assert out.foreign_streak == 5
    legacy = ff.stock_flows(_flow_frame(total=total), "2026-10-14")
    assert legacy.foreign_streak == -20 and legacy.foreign_sell_days_10 == 10


@pytest.mark.unit
def test_market_foreign_sum_caps_a_single_block_deal_day():
    dates = pd.bdate_range(end="2026-10-14", periods=40)
    net = [5e11 if i % 2 else -4e11 for i in range(40)]
    net[-2] = -9.735e12                                  # CafeF 2026-10-09 style spike
    flows = pd.DataFrame({"Date": dates, "ForeignNet": net, "PropNet": [0.0] * 40})
    out = ff.market_flows(flows, None, None, None, "2026-10-14")
    plain = sum(net[-20:])
    assert out.foreign_net_20 > plain                    # the spike is clipped...
    assert out.foreign_net_20 < 0                         # ...but still counts as a heavy sell day
    assert out.foreign_sell_days_15 == sum(1 for v in net[-15:] if v < 0)


# ─── features.calc_liquidity: thin means low traded VALUE ────────────────────


@pytest.mark.unit
def test_penny_stock_with_many_shares_is_thin():
    liq = feat.calc_liquidity([130_000] * 21, closes=[3_300] * 21)   # ~0,43 tỷ/phiên (TLH)
    assert liq.is_thin is True
    assert liq.avg_value_20 == pytest.approx(130_000 * 3_300)


@pytest.mark.unit
def test_high_priced_stock_with_few_shares_is_not_thin():
    liq = feat.calc_liquidity([56_000] * 21, closes=[140_600] * 21)  # ~7,9 tỷ/phiên (BMP)
    assert liq.is_thin is False


@pytest.mark.unit
def test_liquidity_without_prices_keeps_the_share_threshold():
    assert feat.calc_liquidity([56_000] * 21).is_thin is True
    assert feat.calc_liquidity([130_000] * 21).is_thin is False
    assert feat.calc_liquidity([56_000] * 21).avg_value_20 is None


@pytest.mark.unit
def test_liquidity_value_uses_the_same_prior_window():
    # The last session (huge value) is excluded, as for avg_volume_20.
    vols = [10_000] * 20 + [10_000_000]
    liq = feat.calc_liquidity(vols, closes=[10_000] * 21)
    assert liq.avg_value_20 == pytest.approx(1e8)
    assert liq.is_thin is True


# ─── validation: the Tết break is not stale ──────────────────────────────────


def _bdays_ending(last: str, n: int = 60) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=last, periods=n)]


@pytest.mark.unit
def test_reopening_day_after_tet_is_not_stale():
    dates = _bdays_ending("2026-02-13")
    closes = [10.0 + i * 0.01 for i in range(len(dates))]
    now = datetime(2026, 2, 23, 15, 0, tzinfo=VN)
    q = val.validate_ohlcv(closes, closes, closes, closes, dates, now=now)
    assert q.is_stale is False
    assert q.status == "ok"


@pytest.mark.unit
def test_two_weeks_old_frame_is_stale():
    now = datetime(2026, 7, 15, 15, 0, tzinfo=VN)
    dates = _bdays_ending((now - timedelta(days=14)).strftime("%Y-%m-%d"))
    closes = [10.0] * len(dates)
    q = val.validate_ohlcv(closes, closes, closes, closes, dates, now=now)
    assert q.is_stale is True


@pytest.mark.unit
def test_eight_missing_weekdays_is_stale_even_within_twelve_days():
    now = datetime(2026, 7, 15, 15, 0, tzinfo=VN)                   # Wed
    dates = _bdays_ending("2026-07-03")                               # Fri, 12 days before
    closes = [10.0] * len(dates)
    assert val.validate_ohlcv(closes, closes, closes, closes, dates, now=now).is_stale is True


# ─── sector: peers are aligned by date, stale peers are dropped ──────────────


def _ohlcv(dates, closes):
    return pd.DataFrame({"Date": pd.to_datetime(dates), "Close": closes})


@pytest.mark.unit
def test_suspended_peer_does_not_make_the_sector_beat_vnindex(monkeypatch):
    days = list(pd.bdate_range(end="2026-10-14", periods=80))
    falling = [100.0] * 57 + [100.0 * (1 - 0.08 * (i + 1) / 23) for i in range(23)]  # -8% over 1M
    frames = {
        "VCB": _ohlcv(days[:-6], [100.0] * 74),          # suspended for the last 6 sessions
        "BID": _ohlcv(days, falling),
        "CTG": _ohlcv(days, falling),
    }

    def fake_load(code, as_of):
        if code not in frames:
            raise RuntimeError("no data")
        return frames[code]

    monkeypatch.setattr("tradingagents.dataflows.ohlcv.load_ohlcv", fake_load)
    vni = falling
    perf = sector.sector_performance("TCB", "2026-10-14", vni)
    assert perf.sample == 2
    assert perf.trend_1m == pytest.approx(feat.trend_pct(falling[-23:]), abs=0.01)
    assert perf.vs_vnindex_1m == pytest.approx(0.0, abs=0.01)


@pytest.mark.unit
def test_short_history_peer_is_left_out_of_trend_3m(monkeypatch):
    days = list(pd.bdate_range(end="2026-10-14", periods=80))
    flat = [100.0] * 80
    frames = {
        "VCB": _ohlcv(days, flat),
        "BID": _ohlcv(days, flat),
        "CTG": _ohlcv(days[-30:], [100.0] * 7 + [150.0] * 23),   # newly listed, +50% early on
    }
    monkeypatch.setattr("tradingagents.dataflows.ohlcv.load_ohlcv",
                        lambda code, as_of: frames[code] if code in frames else (_ for _ in ()).throw(RuntimeError()))
    perf = sector.sector_performance("TCB", "2026-10-14", None)
    assert perf.trend_3m == pytest.approx(0.0)        # CTG has no bar 65 sessions back
    assert perf.trend_1m == pytest.approx(0.0)        # its last 22 sessions are flat
    assert perf.sample == 3


@pytest.mark.unit
def test_peer_with_a_mid_window_halt_is_aligned_by_date(monkeypatch):
    days = list(pd.bdate_range(end="2026-10-14", periods=80))
    ramp = [100.0 + i for i in range(80)]
    halted_days = days[:60] + days[65:]                 # 5 sessions missing mid-window
    halted = [100.0 + i for i in range(80) if not 60 <= i < 65]
    frames = {"VCB": _ohlcv(days, ramp), "BID": _ohlcv(halted_days, halted)}
    monkeypatch.setattr("tradingagents.dataflows.ohlcv.load_ohlcv",
                        lambda code, as_of: frames[code] if code in frames else (_ for _ in ()).throw(RuntimeError()))
    perf = sector.sector_performance("TCB", "2026-10-14", None)
    assert perf.trend_1m == pytest.approx(feat.trend_pct(ramp[-23:]), abs=0.01)
