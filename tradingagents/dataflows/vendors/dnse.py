"""DNSE (Entrade) public price feeds: daily OHLCV and the latest matched price.

- Chart API ``/chart-api/v2/ohlcs/{stock|index}``: daily bars between two epoch
  seconds. Stock prices come in thousands of VND (57.1 = 57.100đ) and are
  scaled to VND here; index points keep their decimals. Prices are already
  adjusted for dividends and bonus issues by DNSE. HNX and UPCoM composite
  indices are the symbols ``HNX`` / ``UPCOM``.
- Price API (GraphQL): the last matched price of today's session, stocks only.

The chart feed returns today's forming bar during the session; dropping it is
the loader's job, which knows the run date.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import http
from tradingagents.market import vn_rules

OHLC_BASE = "https://services.entrade.com.vn/chart-api/v2/ohlcs"
TICK_API = "https://api.dnse.com.vn/price-api/query"
PRICE_SCALE = 1000
VENDOR = "DNSE"


def _epoch(day: str, *, end: bool = False) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=vn_rules.VN_TZ)
    if end:
        dt += timedelta(days=1)
    return int(dt.timestamp())


def fetch_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily bars ``[start_date, end_date]`` as Date/Open/High/Low/Close/Volume in VND.

    Raises NoMarketDataError when DNSE knows no such symbol or has no bar in the
    window, VendorUnavailableError when the request itself fails.
    """
    is_index = vn_rules.is_index(symbol)
    code = vn_rules.INDEX_ALIASES.get(symbol, symbol)
    response = http.request(
        "GET",
        f"{OHLC_BASE}/{'index' if is_index else 'stock'}",
        vendor=VENDOR,
        params={
            "from": _epoch(start_date),
            "to": _epoch(end_date, end=True),
            "symbol": code,
            "resolution": "1D",
        },
        passthrough=(400, 404),
    )
    if response.status_code in (400, 404):
        raise NoMarketDataError(symbol, code, "DNSE does not know this symbol")
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get("t"):
        raise NoMarketDataError(symbol, code, f"DNSE has no bars between {start_date} and {end_date}")
    return bars_to_frame(data, scale=1 if is_index else PRICE_SCALE, index=is_index)


def bars_to_frame(data: dict, *, scale: float, index: bool) -> pd.DataFrame:
    """DNSE's column arrays (t/o/h/l/c/v) as a clean, date-sorted frame.

    A bar that fails to parse or has no positive close is dropped alone; a
    missing high/low/open falls back to the close.
    """
    t, o, h, lo, c, v = (data.get(k) or [] for k in ("t", "o", "h", "l", "c", "v"))
    rows = []
    for i, ts in enumerate(t):
        try:
            close = float(c[i])
            if not close > 0:
                continue
            open_ = float(o[i]) if i < len(o) and o[i] else close
            high = float(h[i]) if i < len(h) and h[i] else close
            low = float(lo[i]) if i < len(lo) and lo[i] else close
            volume = float(v[i]) if i < len(v) and v[i] else 0.0
            day = datetime.fromtimestamp(int(ts), tz=vn_rules.VN_TZ).strftime("%Y-%m-%d")
        except (IndexError, TypeError, ValueError):
            continue
        rows.append((day, open_ * scale, high * scale, low * scale, close * scale, volume))
    frame = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    if frame.empty:
        return frame
    frame = frame.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
    prices = ["Open", "High", "Low", "Close"]
    frame[prices] = frame[prices].round(2 if index else 0)
    return frame


def fetch_last_match(symbol: str, day: str) -> float | None:
    """Last matched price (VND) of a stock in the session of ``day``, if any."""
    if vn_rules.is_index(symbol):
        return None
    query = (
        "query GetKrxTicksBySymbols {GetKrxTicksBySymbols("
        f'symbols: "{symbol}", date: "{day}", limit: 1, board: 2) {{ticks {{matchPrice}}}}}}'
    )
    data = http.post_json(
        TICK_API,
        vendor=VENDOR,
        json={"operationName": "GetKrxTicksBySymbols", "query": query},
        timeout=8.0,
        retries=0,
    )
    try:
        ticks = data["data"]["GetKrxTicksBySymbols"]["ticks"]
        price = float(ticks[0]["matchPrice"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return round(price * PRICE_SCALE) if price > 0 else None
