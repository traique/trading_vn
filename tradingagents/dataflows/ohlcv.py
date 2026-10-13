"""Daily OHLCV for Vietnamese stocks and indices, cached and point-in-time.

Sources are tried in the order configured in ``data_vendors.core_stock_apis``
(default ``dnse,vci``). Every frame leaving this module:

- is in VND (stocks) or index points, adjusted by the source for dividends and
  bonus issues as of today;
- holds no row after the requested date (no look-ahead);
- holds no forming bar: during today's session (9:00-15:00) the provider's
  bar for today is still moving, so a run dated today ends at yesterday's close;
- is rejected as stale when its latest row is far older than the requested date.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

import pandas as pd

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import locked, replace_file
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.market import vn_rules

logger = logging.getLogger(__name__)

# A latest row this many calendar days before the requested date is stale. Long
# enough to span Tết (up to 9 days without a session), short enough to catch a
# frame that stopped updating.
MAX_OHLCV_STALE_DAYS = 12

# How long a same-day download may be reused for a run dated today, so an
# intraday run picks up the close soon after the session ends.
OHLCV_CACHE_TTL_SECONDS = 900

HISTORY_YEARS = 5
COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def _fetchers() -> dict:
    from tradingagents.dataflows.vendors import dnse, vci

    return {"dnse": dnse.fetch_ohlcv, "vci": vci.fetch_ohlcv}


def configured_sources() -> list[str]:
    """The price sources of the current config, in order."""
    raw = get_config().get("data_vendors", {}).get("core_stock_apis", "dnse,vci")
    sources = [s.strip() for s in str(raw).split(",") if s.strip() and s.strip() != "default"]
    known = _fetchers()
    return [s for s in sources if s in known] or list(known)


def _today() -> pd.Timestamp:
    return pd.Timestamp(vn_rules.now_vn().date())


def _cache_file(canonical: str, source: str) -> str:
    directory = get_config()["data_cache_dir"]
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{safe_ticker_component(canonical)}-{source}-ohlcv.csv")


def _cache_is_fresh(path: str, as_of: pd.Timestamp, today: pd.Timestamp) -> bool:
    """A download serves the day it was made; a run dated today also refetches after the TTL."""
    written = pd.Timestamp.fromtimestamp(os.path.getmtime(path))
    if written.normalize() != pd.Timestamp.now().normalize():
        return False
    if as_of < today:
        return True
    return (pd.Timestamp.now() - written).total_seconds() <= OHLCV_CACHE_TTL_SECONDS


def _download(canonical: str, source: str) -> pd.DataFrame:
    today = _today()
    start = (today - pd.DateOffset(years=HISTORY_YEARS)).strftime("%Y-%m-%d")
    frame = _fetchers()[source](canonical, start, today.strftime("%Y-%m-%d"))
    if frame is None or frame.empty:
        raise NoMarketDataError(canonical, canonical, f"{source} returned no price rows")
    return frame[COLUMNS]


def _read_source(canonical: str, source: str, as_of: pd.Timestamp) -> pd.DataFrame:
    """The cached bars of one source, downloaded first when stale or missing.

    The analysts, the quant engine and the sector peers ask for the same symbol
    at the same time, so one caller downloads while the others wait and then
    read its file, instead of each downloading and replacing it in turn.
    """
    path = _cache_file(canonical, source)
    with locked(path):
        if os.path.exists(path) and _cache_is_fresh(path, as_of, _today()):
            cached = pd.read_csv(path, encoding="utf-8")
            if not cached.empty and set(COLUMNS) <= set(cached.columns):
                return cached[COLUMNS]
        frame = _download(canonical, source)
        replace_file(path, lambda temp: frame.to_csv(temp, index=False, encoding="utf-8"))
        return frame


def drop_forming_bar(frame: pd.DataFrame, as_of: pd.Timestamp, now=None) -> pd.DataFrame:
    """Drop today's bar while today's session can still move it."""
    if frame.empty:
        return frame
    now = now or vn_rules.now_vn()
    today = pd.Timestamp(now.date())
    if as_of >= today and frame["Date"].iloc[-1] == today and vn_rules.is_market_hours(now):
        return frame.iloc[:-1]
    return frame


def _clean(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
    frame = frame.dropna(subset=["Date"])
    for col in COLUMNS[1:]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Close"])
    frame = frame[frame["Close"] > 0]
    return frame.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)


def assert_not_stale(frame: pd.DataFrame, as_of_date: str, symbol: str) -> None:
    """Reject a frame whose latest row is far older than ``as_of_date``."""
    if frame.empty:
        return
    requested = pd.Timestamp(as_of_date).normalize()
    latest = pd.Timestamp(frame["Date"].max()).normalize()
    stale_days = (requested - latest).days
    if stale_days > MAX_OHLCV_STALE_DAYS:
        raise NoMarketDataError(
            symbol, normalize_symbol(symbol),
            f"latest row is {latest.date()}, {stale_days} days before {requested.date()} "
            "(stale or suspended) - refusing to use it",
        )


def load_ohlcv(
    symbol: str,
    as_of_date: str,
    fill_gaps: bool = True,
    sources: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Daily bars up to and including ``as_of_date`` (closed sessions only).

    ``fill_gaps`` forward-fills an occasional missing open/high/low/volume so
    indicators compute on a continuous series; pass False to read the values
    as reported. Raises NoMarketDataError when no source has usable rows and
    VendorUnavailableError when every source failed to answer.
    """
    canonical = normalize_symbol(symbol)
    safe_ticker_component(canonical)
    as_of = pd.Timestamp(as_of_date).normalize()
    last_no_data: NoMarketDataError | None = None
    last_unavailable: VendorUnavailableError | None = None
    for source in list(sources or configured_sources()):
        try:
            frame = _clean(_read_source(canonical, source, as_of))
        except NoMarketDataError as exc:
            last_no_data = exc
            continue
        except VendorUnavailableError as exc:
            logger.warning("Price source %s unavailable for %s: %s", source, canonical, exc)
            last_unavailable = exc
            continue
        frame = drop_forming_bar(frame[frame["Date"] <= as_of], as_of)
        if frame.empty:
            last_no_data = NoMarketDataError(symbol, canonical, f"no closed session on or before {as_of.date()}")
            continue
        try:
            assert_not_stale(frame, as_of_date, symbol)
        except NoMarketDataError as exc:
            last_no_data = exc
            continue
        if fill_gaps:
            frame[COLUMNS[1:]] = frame[COLUMNS[1:]].ffill().bfill()
        frame.attrs["source"] = source
        return frame.reset_index(drop=True)
    if last_unavailable is not None and last_no_data is None:
        raise last_unavailable
    raise last_no_data or NoMarketDataError(symbol, canonical, "no price source configured")


def get_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """Daily closes from ``start_date`` up to, not including, ``end_date``.

    Used to settle past decisions, so it may read bars after the decision date;
    it never serves a forming bar. Returns an empty Series when there is no data.
    """
    last_day = (pd.Timestamp(end_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        frame = load_ohlcv(symbol, last_day, fill_gaps=False)
    except (NoMarketDataError, VendorUnavailableError):
        return pd.Series(dtype=float)
    frame = frame[frame["Date"] >= pd.Timestamp(start_date)]
    return pd.Series(frame["Close"].to_numpy(), index=pd.DatetimeIndex(frame["Date"]), name="Close")


def _fmt(value: float, index: bool) -> str:
    return f"{value:.2f}" if index else f"{value:.0f}"


def get_stock_data(symbol: str, start_date: str, end_date: str, source: str | None = None) -> str:
    """OHLCV rows ``[start_date, end_date]`` as CSV text for the agents."""
    canonical = normalize_symbol(symbol)
    frame = load_ohlcv(canonical, end_date, fill_gaps=False, sources=[source] if source else None)
    rows = frame[frame["Date"] >= pd.Timestamp(start_date)]
    if rows.empty:
        raise NoMarketDataError(symbol, canonical, f"no session between {start_date} and {end_date}")
    index = vn_rules.is_index(canonical)
    out = rows.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    for col in ("Open", "High", "Low", "Close"):
        out[col] = out[col].map(lambda v: _fmt(v, index))
    out["Volume"] = out["Volume"].map(lambda v: f"{v:.0f}")
    unit = "điểm" if index else "VND (đồng), giá đã điều chỉnh cổ tức/thưởng theo nguồn"
    header = (
        f"# Dữ liệu giá {canonical} từ {start_date} đến {end_date} "
        f"(nguồn {frame.attrs.get('source', source or '?').upper()})\n"
        f"# Đơn vị giá: {unit}; khối lượng: cổ phiếu\n"
        f"# Số phiên: {len(out)}; chỉ gồm phiên đã đóng cửa\n\n"
    )
    return header + out.to_csv(index=False)
