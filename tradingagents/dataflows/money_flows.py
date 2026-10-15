"""Money-flow history, cached on disk and read point-in-time.

Foreign and proprietary (tự doanh) trading per stock, the same for the whole
market, ETF creations/redemptions and HOSE breadth. Each series is kept in a
CSV under ``<data_cache_dir>/flows`` and extended incrementally: closed
sessions never change, so a day is downloaded once.

Two ways to fill a series:

- ``backfill=False`` (an analysis asked for it): only the recent window is
  fetched (``RECENT_DAYS``), so a first request stays a few calls long.
- ``backfill=True`` (the nightly job): the full ``HISTORY_DAYS`` are fetched,
  older days included, for the calibration to backtest on.

Every loader returns rows dated on or before ``as_of`` only. A vendor failure
leaves the cached rows in place and is logged; the caller sees whatever was
cached (possibly nothing) and treats missing flow data as "no signal".
Today's flows are published after the close, so before 15:30 a run dated
today reads up to yesterday.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import time as dtime

import pandas as pd

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorError, VendorUnavailableError
from tradingagents.dataflows.files import locked, replace_file
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.market import vn_rules

logger = logging.getLogger(__name__)

HISTORY_DAYS = 900          # calendar days kept for calibration (~2.5 years)
RECENT_DAYS = 120           # what an analysis fetches when the cache is empty
# Breadth costs one call per session (each a few seconds): enough history for
# a year of calibration plus the 50-session average, not more.
BREADTH_HISTORY_DAYS = 760
RECHECK_SECONDS = 3600      # a day already asked for and not served is not asked again sooner
PUBLISHED_AFTER = dtime(15, 30)

# Exchange-traded funds whose creations/redemptions show domestic and foreign
# money moving into the VN30 / Diamond / VNFinSelect baskets.
ETF_CODES = ("E1VFVN30", "FUEVFVND", "FUESSVFL", "FUESSV30", "FUEKIV30")

# Last known VN30 members, used when the basket cannot be fetched.
VN30_FALLBACK = (
    "ACB", "BCM", "BID", "BSR", "CTG", "FPT", "GAS", "GVR", "HDB", "HPG", "LPB", "MBB",
    "MSN", "MWG", "PLX", "SAB", "SHB", "SSB", "SSI", "STB", "TCB", "TPB", "VCB", "VHM",
    "VIB", "VIC", "VJC", "VNM", "VPB", "VRE",
)


def _vendor():
    from tradingagents.dataflows.vendors import flows

    return flows


# A host that just failed is not asked again for a while: an analysis reads
# several flow series and each failed request would otherwise wait out its retries.
BREAKER_SECONDS = 300
_down_until: dict[str, float] = {}


def reset_breaker() -> None:
    _down_until.clear()


def _guarded(host: str, call: Callable[[], pd.DataFrame]):
    if time.time() < _down_until.get(host, 0.0):
        raise VendorUnavailableError(f"{host} failed recently; not asked again yet")
    try:
        return call()
    except VendorError:
        _down_until[host] = time.time() + BREAKER_SECONDS
        raise


def _dir() -> str:
    path = os.path.join(get_config()["data_cache_dir"], "flows")
    os.makedirs(path, exist_ok=True)
    return path


def _today() -> pd.Timestamp:
    return pd.Timestamp(vn_rules.now_vn().date())


def published_through() -> pd.Timestamp:
    """The last day whose flows are out: today after 15:30, else yesterday."""
    now = vn_rules.now_vn()
    today = pd.Timestamp(now.date())
    return today if now.time() >= PUBLISHED_AFTER else today - pd.Timedelta(days=1)


def _read(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path, encoding="utf-8")
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    if frame.empty or "Date" not in frame:
        return pd.DataFrame()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
    return frame.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)


def _meta(path: str) -> dict:
    try:
        with open(f"{path}.json", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _write_text(path: str, text: str) -> None:
    def write(temp: str) -> None:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)

    replace_file(path, write)


def _write_meta(path: str, meta: dict) -> None:
    _write_text(f"{path}.json", json.dumps(meta))


def _ensure(path: str, fetch: Callable[[str, str], pd.DataFrame], backfill: bool) -> pd.DataFrame:
    """The cached series, extended to the last published day (and back in time if backfilling)."""
    end = published_through()
    first_wanted = end - pd.Timedelta(days=HISTORY_DAYS if backfill else RECENT_DAYS)
    with locked(path):
        cached = _read(path)
        meta = _meta(path)
        ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        if cached.empty:
            ranges.append((first_wanted, end))
        else:
            first, last = cached["Date"].iloc[0], cached["Date"].iloc[-1]
            asked_end = meta.get("asked_end")
            recently_asked = (
                asked_end == end.strftime("%Y-%m-%d")
                and time.time() - float(meta.get("asked_at", 0)) < RECHECK_SECONDS
            )
            if last < end and not recently_asked:
                ranges.append((last + pd.Timedelta(days=1), end))
            if backfill and first > first_wanted + pd.Timedelta(days=7) and not meta.get("history_complete"):
                ranges.append((first_wanted, first - pd.Timedelta(days=1)))
        if not ranges:
            return cached
        parts = [cached]
        failed = False
        for lo, hi in ranges:
            try:
                parts.append(fetch(lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")))
            except VendorError as exc:
                logger.warning("Flow data %s %s..%s unavailable: %s", os.path.basename(path), lo.date(), hi.date(), exc)
                failed = True
        frames = [p for p in parts if p is not None and not p.empty]
        merged = (
            pd.concat(frames).drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
            if frames else pd.DataFrame()
        )
        if not merged.empty:
            replace_file(path, lambda temp: merged.to_csv(temp, index=False, encoding="utf-8"))
        if not failed:
            meta.update(asked_end=end.strftime("%Y-%m-%d"), asked_at=time.time())
            if backfill:
                meta["history_complete"] = True
            _write_meta(path, meta)
        return merged


def _upto(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[frame["Date"] <= pd.Timestamp(as_of)].reset_index(drop=True)


def load_stock_flows(symbol: str, as_of: str, *, backfill: bool = False) -> pd.DataFrame:
    """Foreign and proprietary flows of one stock (or ETF), one row per session, up to ``as_of``.

    Columns: Date, ForeignBuy, ForeignSell, ForeignNet, MatchValue, Close,
    TotalShares, ForeignRoomPct, PropNet (NaN before proprietary data exists).
    """
    code = safe_ticker_component(symbol.upper())
    vendor = _vendor()
    foreign = _ensure(os.path.join(_dir(), f"{code}-foreign.csv"),
                      lambda s, e: _guarded("VCI", lambda: vendor.fetch_stock_flows(code, s, e)), backfill)
    if foreign.empty:
        return foreign
    if code in ETF_CODES:
        foreign["PropNet"] = float("nan")
        return _upto(foreign, as_of)
    prop = _ensure(os.path.join(_dir(), f"{code}-prop.csv"),
                   lambda s, e: _guarded("VCI", lambda: vendor.fetch_proprietary_flows(code, s, e)), backfill)
    if not prop.empty:
        foreign = foreign.merge(prop[["Date", "PropNet"]], on="Date", how="left")
    else:
        foreign["PropNet"] = float("nan")
    return _upto(foreign, as_of)


def load_market_flows(as_of: str, *, backfill: bool = False) -> pd.DataFrame:
    """Market-wide daily foreign and proprietary net value (VND), up to ``as_of``."""
    frame = _ensure(os.path.join(_dir(), "market-flows.csv"),
                    lambda s, e: _guarded("CafeF", lambda: _vendor().fetch_market_flows(s, e)), backfill)
    return _upto(frame, as_of)


def load_etf_flows(as_of: str, *, backfill: bool = False) -> pd.DataFrame:
    """Daily net creation value (VND) summed over the tracked ETFs, up to ``as_of``.

    A creation adds fund units, so ``ΔTotalShares × Close`` is the money the
    fund had to put into its basket that day (negative for a redemption).
    """
    parts = []
    for code in ETF_CODES:
        try:
            frame = load_stock_flows(code, as_of, backfill=backfill)
        except Exception as exc:  # noqa: BLE001 - one fund missing must not sink the series
            logger.warning("ETF %s flows unavailable: %s", code, exc)
            continue
        if frame.empty or "TotalShares" not in frame:
            continue
        units = frame["TotalShares"].astype(float)
        value = units.diff() * frame["Close"].astype(float)
        parts.append(pd.DataFrame({"Date": frame["Date"], code: value}))
    if not parts:
        return pd.DataFrame(columns=["Date", "EtfNet"])
    merged = parts[0]
    for part in parts[1:]:
        merged = merged.merge(part, on="Date", how="outer")
    merged = merged.sort_values("Date").reset_index(drop=True)
    merged["EtfNet"] = merged.drop(columns=["Date"]).sum(axis=1, min_count=1)
    return merged[["Date", "EtfNet"]]


# ─── Breadth ──────────────────────────────────────────────────────────────────


def _breadth_paths() -> tuple[str, str]:
    return os.path.join(_dir(), "hose-breadth.csv"), os.path.join(_dir(), "hose-closes.csv")


def _sessions(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """VNINDEX sessions in [start, end] (holidays excluded)."""
    from tradingagents.dataflows.ohlcv import load_ohlcv

    bars = load_ohlcv(vn_rules.BENCHMARK, end.strftime("%Y-%m-%d"))
    dates = pd.to_datetime(bars["Date"]).dt.normalize()
    return [d for d in dates if start <= d <= end]


def _breadth_row(day: pd.Timestamp, bars: pd.DataFrame) -> dict:
    change = bars["Change"].fillna(0)
    return {
        "Date": day,
        "Advances": int((change > 0).sum()),
        "Declines": int((change < 0).sum()),
        "Unchanged": int((change == 0).sum()),
        "Ceilings": int(((bars["Close"] >= bars["Ceiling"]) & (bars["Ceiling"] > 0)).sum()),
        "Floors": int(((bars["Close"] <= bars["Floor"]) & (bars["Floor"] > 0)).sum()),
        "Total": int(len(bars)),
    }


def _merge_breadth(rows: list[dict], close_rows: list[pd.Series]) -> None:
    """Add fetched sessions to the two breadth files, under their lock."""
    breadth_path, closes_path = _breadth_paths()
    with locked(breadth_path):
        breadth, closes = _read(breadth_path), _read(closes_path)
        breadth = pd.concat([breadth, pd.DataFrame(rows)]).drop_duplicates("Date", keep="last")
        breadth = breadth.sort_values("Date").reset_index(drop=True)
        new_closes = pd.DataFrame(close_rows)
        new_closes.index.name = "Date"
        closes = pd.concat([closes, new_closes.reset_index()]).drop_duplicates("Date", keep="last")
        closes = closes.sort_values("Date").reset_index(drop=True)
        replace_file(breadth_path, lambda t: breadth.to_csv(t, index=False, encoding="utf-8"))
        replace_file(closes_path, lambda t: closes.to_csv(t, index=False, encoding="utf-8"))


BREADTH_FLUSH = 20   # sessions fetched between two saves (a long backfill survives a restart)


def load_breadth(as_of: str, *, backfill: bool = False) -> pd.DataFrame:
    """HOSE breadth per session up to ``as_of``.

    Columns: Date, Advances, Declines, Unchanged, Ceilings, Floors, Total,
    PctAboveMA50 (share of stocks closing above their 50-session average,
    NaN until 50 sessions are cached), NewHighs20 (stocks at a 20-session
    closing high).

    One call per session, a few seconds each: an analysis only tops up the
    latest five sessions, the nightly backfill brings the history, saving as
    it goes. The files are locked only while written, so an analysis never
    waits for a backfill.
    """
    breadth_path, closes_path = _breadth_paths()
    end = published_through()
    first_wanted = end - pd.Timedelta(days=BREADTH_HISTORY_DAYS if backfill else RECENT_DAYS)
    breadth = _read(breadth_path)
    have = set(breadth["Date"]) if not breadth.empty else set()
    meta = _meta(breadth_path)
    recently = (meta.get("asked_end") == end.strftime("%Y-%m-%d")
                and time.time() - float(meta.get("asked_at", 0)) < RECHECK_SECONDS)
    wanted: list[pd.Timestamp] = []
    if backfill or not recently:
        try:
            wanted = [d for d in _sessions(first_wanted, end) if d not in have]
        except VendorError as exc:
            logger.warning("Breadth: VNINDEX sessions unavailable: %s", exc)
    # An analysis tops up the latest five; a backfill goes newest first, so the useful end lands first.
    wanted = list(reversed(wanted)) if backfill else wanted[-5:]
    rows, close_rows, failed = [], [], False
    for day in wanted:
        try:
            bars = _guarded("VNDirect", lambda day=day: _vendor().fetch_hose_day(day.strftime("%Y-%m-%d")))
        except VendorError as exc:
            logger.warning("Breadth for %s unavailable: %s", day.date(), exc)
            failed = True
            break
        if not bars.empty:
            rows.append(_breadth_row(day, bars))
            close_rows.append(pd.Series(bars["AdClose"].to_numpy(), index=bars["Code"], name=day))
        if len(rows) >= BREADTH_FLUSH:
            _merge_breadth(rows, close_rows)
            rows, close_rows = [], []
        time.sleep(0.2)
    if rows:
        _merge_breadth(rows, close_rows)
    if not failed:
        meta.update(asked_end=end.strftime("%Y-%m-%d"), asked_at=time.time())
        _write_meta(breadth_path, meta)
    breadth, closes = _read(breadth_path), _read(closes_path)
    if breadth.empty:
        return breadth
    breadth = breadth.copy()
    if not closes.empty:
        matrix = closes.set_index("Date").sort_index().apply(pd.to_numeric, errors="coerce")
        ma50 = matrix.rolling(50, min_periods=50).mean()
        valid = ma50.notna() & matrix.notna()
        above = ((matrix > ma50) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, float("nan")) * 100
        high20 = matrix.rolling(20, min_periods=20).max()
        new_highs = ((matrix >= high20) & high20.notna()).sum(axis=1)
        extra = pd.DataFrame({"PctAboveMA50": above, "NewHighs20": new_highs}).reset_index()
        breadth = breadth.drop(columns=[c for c in ("PctAboveMA50", "NewHighs20") if c in breadth]).merge(
            extra, on="Date", how="left")
    return _upto(breadth, as_of)


# ─── Baskets ──────────────────────────────────────────────────────────────────


def index_members(group: str = "VN30") -> list[str]:
    """Current members of an index basket, refreshed daily; the last known list on failure."""
    path = os.path.join(_dir(), f"group-{safe_ticker_component(group.upper())}.json")
    cached: dict = {}
    try:
        with open(path, encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        pass
    today = _today().strftime("%Y-%m-%d")
    if cached.get("day") == today and cached.get("codes"):
        return list(cached["codes"])
    try:
        codes = _guarded("VCI", lambda: _vendor().fetch_group(group.upper()))
    except VendorError as exc:
        logger.warning("Basket %s unavailable: %s", group, exc)
        codes = []
    if codes:
        _write_text(path, json.dumps({"day": today, "codes": codes}))
        return codes
    if cached.get("codes"):
        return list(cached["codes"])
    return list(VN30_FALLBACK) if group.upper() == "VN30" else []
