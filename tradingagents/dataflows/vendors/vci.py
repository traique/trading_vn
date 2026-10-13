"""Vietcap (VCI) public REST endpoints, called directly.

The vnstock library wraps these same endpoints; calling them directly avoids
its slow import, console banners and private package index. Endpoints (all
GET unless noted, no cookie or key):

- ``trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart`` (POST): daily bars,
  prices in VND, adjusted for corporate actions. Fallback price source.
- ``trading.vietcap.com.vn/api/price/symbols/getAll``: every listed code with
  its board (HSX/HNX/UPCOM), name and ICB code.
- ``iq.vietcap.com.vn/.../v1/company/details?ticker=``: profile, sector,
  foreign ownership, live price.
- ``.../v1/company/{sym}/statistics-financial``: TTM ratios per quarter
  (pe, pb, roe, ...), oldest first.
- ``.../v1/company/{sym}/financial-statement?section=INCOME_STATEMENT|
  BALANCE_SHEET|CASH_FLOW``: statements by quarter and year, each row with the
  ``publicDate`` it was published - what makes point-in-time reads possible.
- ``.../v1/company/{sym}/financial-statement/metrics``: Vietnamese/English
  labels of every field code (isa*, bsa*, cfa*; banks use isb*/bsb*/cfb*).
- ``.../v1/events``: dividends, share issues, insider and major-holder deals,
  AGMs, each with a public date. ``.../v1/news``: company disclosures.

Verified against live responses on 13/10/2026.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import http
from tradingagents.market import vn_rules

VENDOR = "VCI"
TRADING_BASE = "https://trading.vietcap.com.vn/api"
IQ_BASE = "https://iq.vietcap.com.vn/api/iq-insight-service"
COMPANY_BASE = f"{IQ_BASE}/v1/company"
_HEADERS = {
    "Referer": "https://trading.vietcap.com.vn/",
    "Origin": "https://trading.vietcap.com.vn",
}
STATEMENT_SECTIONS = ("INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW")

_CACHE_TTL = 30 * 60
_CACHE_MAX = 256
_cache: dict[tuple, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(key: tuple, fetch, ttl: float = _CACHE_TTL):
    """Process-wide cache of parsed responses; only successful fetches are kept."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fetch()
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _get(url: str, **kwargs):
    return http.get_json(url, vendor=VENDOR, headers=_HEADERS, **kwargs)


def _data(payload, symbol: str, what: str):
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        raise NoMarketDataError(symbol, symbol, f"VCI returned no {what}")
    return data


# ─── Prices ──────────────────────────────────────────────────────────────────


def fetch_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily bars ``[start_date, end_date]`` as Date/Open/High/Low/Close/Volume in VND."""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    count_back = len(pd.bdate_range(start, end)) + 5
    to_epoch = int((end + pd.Timedelta(days=1)).tz_localize(vn_rules.VN_TZ).timestamp())
    code = vn_rules.INDEX_ALIASES.get(symbol, symbol)
    payload = http.post_json(
        f"{TRADING_BASE}/chart/OHLCChart/gap-chart",
        vendor=VENDOR,
        headers=_HEADERS,
        json={"timeFrame": "ONE_DAY", "symbols": [code], "to": to_epoch, "countBack": count_back},
    )
    if isinstance(payload, dict):
        payload = payload.get("data")
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise NoMarketDataError(symbol, code, "VCI has no bars for this symbol")
    bars = payload[0]
    rows = []
    for i, ts in enumerate(bars.get("t") or []):
        try:
            close = float(bars["c"][i])
            if not close > 0:
                continue
            day = datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d")
            rows.append((
                day,
                float(bars["o"][i] or close),
                float(bars["h"][i] or close),
                float(bars["l"][i] or close),
                close,
                float(bars["v"][i] or 0),
            ))
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    frame = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    frame = frame[(frame["Date"] >= start_date) & (frame["Date"] <= end_date)]
    if frame.empty:
        raise NoMarketDataError(symbol, code, f"VCI has no bars between {start_date} and {end_date}")
    frame = frame.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
    prices = ["Open", "High", "Low", "Close"]
    frame[prices] = frame[prices].round(2 if vn_rules.is_index(symbol) else 0)
    return frame


# ─── Listing ─────────────────────────────────────────────────────────────────


def fetch_listing() -> list[dict]:
    """Every listed code: ``symbol``, ``board`` (HSX/HNX/UPCOM/...), ``type``, names, ``icbCode2``."""
    def fetch():
        payload = http.get_json(
            f"{TRADING_BASE}/price/symbols/getAll", vendor=VENDOR, headers=_HEADERS, timeout=15.0
        )
        if not isinstance(payload, list):
            raise NoMarketDataError("*", "*", "VCI returned no listing")
        return payload

    return _cached(("listing",), fetch, ttl=24 * 3600)


# ─── Company data ────────────────────────────────────────────────────────────


def fetch_details(symbol: str) -> dict:
    """Company profile and live market fields (present-day values only)."""
    return _cached(
        ("details", symbol),
        lambda: _data(_get(f"{COMPANY_BASE}/details", params={"ticker": symbol}), symbol, "company details"),
    )


def fetch_ratios(symbol: str) -> list[dict]:
    """TTM ratio rows per quarter, oldest first (``yearReport``, ``quarter``, ``pe``, ``pb``, ...)."""
    def fetch():
        rows = _data(_get(f"{COMPANY_BASE}/{symbol}/statistics-financial"), symbol, "ratios")
        if not isinstance(rows, list):
            raise NoMarketDataError(symbol, symbol, "VCI returned no ratio rows")
        return sorted(rows, key=lambda r: (r.get("yearReport") or 0, r.get("quarter") or 0))

    return _cached(("ratios", symbol), fetch)


def fetch_statement(symbol: str, section: str) -> dict:
    """``{"quarters": [...], "years": [...]}`` rows of one statement, each with ``publicDate``."""
    if section not in STATEMENT_SECTIONS:
        raise ValueError(f"unknown statement section {section!r}")

    def fetch():
        data = _data(
            _get(f"{COMPANY_BASE}/{symbol}/financial-statement", params={"section": section}),
            symbol,
            section.lower(),
        )
        if not isinstance(data, dict):
            raise NoMarketDataError(symbol, symbol, f"VCI returned no {section.lower()}")
        return {"quarters": data.get("quarters") or [], "years": data.get("years") or []}

    return _cached(("statement", symbol, section), fetch)


def fetch_metric_labels(symbol: str) -> dict[str, list[dict]]:
    """Per section, the ordered field list ``{field, titleVi, titleEn, level, parent}``.

    The field codes depend on the company type (bank, securities, insurer,
    other), so labels are fetched per symbol.
    """
    def fetch():
        data = _data(_get(f"{COMPANY_BASE}/{symbol}/financial-statement/metrics"), symbol, "labels")
        return data if isinstance(data, dict) else {}

    return _cached(("labels", symbol), fetch, ttl=24 * 3600)


def fetch_events(
    symbol: str, from_date: str, to_date: str, codes: str = "DIV,ISS,DDIND,DDINS,DDRP,AGME,AGMR,EGME",
    size: int = 50,
) -> list[dict]:
    """Corporate events between two dates (YYYY-MM-DD), newest first.

    Codes: DIV dividends, ISS share issues, DDIND/DDINS/DDRP insider and
    major-holder deals, AGME/AGMR/EGME shareholder meetings.
    """
    def fetch():
        payload = _get(
            f"{IQ_BASE}/v1/events",
            params={
                "ticker": symbol,
                "fromDate": from_date.replace("-", ""),
                "toDate": to_date.replace("-", ""),
                "eventCode": codes,
                "page": 0,
                "size": size,
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        return (data or {}).get("content") or [] if isinstance(data, dict) else []

    return _cached(("events", symbol, from_date, to_date, codes, size), fetch)


def fetch_news(symbol: str, from_date: str, to_date: str, size: int = 20) -> list[dict]:
    """Company disclosures between two dates (YYYY-MM-DD), newest first."""
    def fetch():
        payload = _get(
            f"{IQ_BASE}/v1/news",
            params={
                "ticker": symbol,
                "fromDate": from_date.replace("-", ""),
                "toDate": to_date.replace("-", ""),
                "languageId": 1,
                "page": 0,
                "size": size,
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        return (data or {}).get("content") or [] if isinstance(data, dict) else []

    return _cached(("news", symbol, from_date, to_date, size), fetch)
