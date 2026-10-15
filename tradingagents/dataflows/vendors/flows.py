"""Money-flow data for Vietnamese stocks: who bought and sold, by day.

All public and keyless, verified against live responses on 14/10/2026:

- Vietcap (VCI) ``iq.vietcap.com.vn/.../v1/company/{SYM}/price-history``:
  per stock and per ETF, daily foreign buy/sell value, foreign room, matched
  value and shares outstanding (ETF creations/redemptions show as changes in
  ``totalShares``). History back to 2006. Empty for indices.
- Vietcap ``.../v1/company/{SYM}/proprietary-history``: per stock, daily
  proprietary (tự doanh) buy/sell value. History from about 2019.
- CafeF ``du-lieu/Ajax/PageNew/DataHistory/GDKhoiNgoai.ashx`` and
  ``GDTuDoanh.ashx`` with ``Symbol=VNINDEX``: market-wide foreign and
  proprietary net value. Dates are MM/dd/yyyy; a page holds 30 rows.
- VNDirect ``api-finfo.vndirect.com.vn/v4/stock_prices``: every HOSE stock's
  bar for one day, from which breadth (advances, declines, ceilings) and the
  share of stocks above their averages are computed.
- Vietcap ``trading.vietcap.com.vn/api/price/symbols/getByGroup?group=VN30``:
  the current members of an index basket.

Values are in VND. These are the endpoints the vendors' own websites call,
not documented APIs: every failure raises VendorUnavailableError and callers
treat the data as missing rather than as a fact about the market.
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import http

VCI_COMPANY = "https://iq.vietcap.com.vn/api/iq-insight-service/v1/company"
VCI_GROUP = "https://trading.vietcap.com.vn/api/price/symbols/getByGroup"
VCI_HEADERS = {"Referer": "https://trading.vietcap.com.vn/", "Origin": "https://trading.vietcap.com.vn"}
CAFEF_BASE = "https://cafef.vn/du-lieu/Ajax/PageNew/DataHistory"
CAFEF_HEADERS = {"Referer": "https://cafef.vn/"}
VNDIRECT_PRICES = "https://api-finfo.vndirect.com.vn/v4/stock_prices"

PAGE_SIZE = 500
CAFEF_PAGE = 30
PAUSE_SECONDS = 0.3      # between paged calls: the hosts reset bursts

STOCK_FLOW_COLUMNS = ["Date", "ForeignBuy", "ForeignSell", "ForeignNet", "MatchValue",
                      "Close", "TotalShares", "ForeignRoomPct"]
PROP_COLUMNS = ["Date", "PropBuy", "PropSell", "PropNet"]
MARKET_FLOW_COLUMNS = ["Date", "ForeignNet", "PropNet"]
BREADTH_COLUMNS = ["Date", "Advances", "Declines", "Unchanged", "Ceilings", "Floors", "Total"]


def _year_chunks(start: str, end: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[start, end] split into calendar-year pieces (the hosts clip longer ranges)."""
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    chunks = []
    while lo <= hi:
        top = min(hi, pd.Timestamp(year=lo.year, month=12, day=31))
        chunks.append((lo, top))
        lo = top + pd.Timedelta(days=1)
    return chunks


def _vci_pages(path: str, start: str, end: str) -> list[dict]:
    rows: list[dict] = []
    for lo, hi in _year_chunks(start, end):
        page = 0
        while True:
            body = http.get_json(
                f"{VCI_COMPANY}/{path}", vendor="VCI", headers=VCI_HEADERS, timeout=20,
                params={"timeFrame": "ONE_DAY", "page": page, "size": PAGE_SIZE,
                        "fromDate": lo.strftime("%Y%m%d"), "toDate": hi.strftime("%Y%m%d")},
            )
            content = ((body or {}).get("data") or {}).get("content") or []
            rows.extend(content)
            if len(content) < PAGE_SIZE:
                break
            page += 1
            time.sleep(PAUSE_SECONDS)
        time.sleep(PAUSE_SECONDS)
    return rows


def _num(value) -> float:
    try:
        return float(value) if value is not None else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=columns)
    if frame.empty:
        return frame
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
    frame = frame.dropna(subset=["Date"])
    return frame.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)


def fetch_stock_flows(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Daily foreign trading of one stock or ETF between two dates (inclusive)."""
    rows = []
    for item in _vci_pages(f"{symbol}/price-history", start, end):
        buy, sell = _num(item.get("foreignBuyValueTotal")), _num(item.get("foreignSellValueTotal"))
        rows.append({
            "Date": item.get("tradingDate"),
            "ForeignBuy": buy,
            "ForeignSell": sell,
            "ForeignNet": buy - sell,
            "MatchValue": _num(item.get("totalMatchValue")),
            "Close": _num(item.get("closePrice")),
            "TotalShares": _num(item.get("totalShares")),
            # Room still open to foreigners, % of shares (VCI gives a fraction).
            "ForeignRoomPct": _num(item.get("foreignAvailablePercentage")) * 100,
        })
    return _frame(rows, STOCK_FLOW_COLUMNS)


def fetch_proprietary_flows(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Daily proprietary (tự doanh) trading of one stock between two dates."""
    rows = [{
        "Date": item.get("tradingDate"),
        "PropBuy": _num(item.get("totalBuyTradeValue")),
        "PropSell": _num(item.get("totalSellTradeValue")),
        "PropNet": _num(item.get("totalTradeNetValue")),
    } for item in _vci_pages(f"{symbol}/proprietary-history", start, end)]
    return _frame(rows, PROP_COLUMNS)


def _cafef_rows(endpoint: str, start: str, end: str) -> list[dict]:
    out: list[dict] = []
    for lo, hi in _year_chunks(start, end):
        page = 1
        while True:
            body = http.get_json(
                f"{CAFEF_BASE}/{endpoint}", vendor="CafeF", headers=CAFEF_HEADERS, timeout=30,
                params={"Symbol": "VNINDEX", "StartDate": lo.strftime("%m/%d/%Y"),
                        "EndDate": hi.strftime("%m/%d/%Y"), "PageIndex": page, "PageSize": CAFEF_PAGE},
            )
            data = (body or {}).get("Data") or {}
            rows = data.get("Data") or []
            if isinstance(rows, dict):          # GDTuDoanh nests its list one level deeper
                rows = rows.get("ListDataTudoanh") or []
            out.extend(rows)
            total = int(data.get("TotalCount") or 0)
            if not rows or page * CAFEF_PAGE >= total:
                break
            page += 1
            time.sleep(PAUSE_SECONDS)
        time.sleep(PAUSE_SECONDS)
    return out


def _cafef_date(text) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(datetime.strptime(str(text).strip(), "%d/%m/%Y"))
    except ValueError:
        return None


def fetch_market_flows(start: str, end: str) -> pd.DataFrame:
    """Market-wide (HOSE) daily foreign and proprietary net value."""
    foreign = {
        _cafef_date(r.get("Ngay")): _num(r.get("GTDGRong"))
        for r in _cafef_rows("GDKhoiNgoai.ashx", start, end)
    }
    prop = {
        _cafef_date(r.get("Date")): _num(r.get("GtMua")) - _num(r.get("GtBan"))
        for r in _cafef_rows("GDTuDoanh.ashx", start, end)
    }
    days = sorted(d for d in set(foreign) | set(prop) if d is not None)
    if not days and (foreign or prop):
        raise VendorUnavailableError("CafeF returned rows without readable dates")
    rows = [{"Date": d, "ForeignNet": foreign.get(d, float("nan")), "PropNet": prop.get(d, float("nan"))}
            for d in days]
    return _frame(rows, MARKET_FLOW_COLUMNS)


def fetch_hose_day(day: str) -> pd.DataFrame:
    """Every HOSE stock's bar for one day: code, adjusted close, change, ceiling/floor hits."""
    body = http.get_json(
        VNDIRECT_PRICES, vendor="VNDirect", timeout=30,
        params={"q": f"floor:HOSE~date:{day}~type:STOCK", "size": 1000},
    )
    rows = (body or {}).get("data") or []
    frame = pd.DataFrame([{
        "Code": r.get("code"),
        "Close": _num(r.get("close")),
        "AdClose": _num(r.get("adClose")),
        "Change": _num(r.get("change")),
        "Ceiling": _num(r.get("ceilingPrice")),
        "Floor": _num(r.get("floorPrice")),
    } for r in rows if r.get("code")])
    return frame


def fetch_group(group: str) -> list[str]:
    """Current member codes of an index basket (VN30, VN100, ...)."""
    body = http.get_json(VCI_GROUP, vendor="VCI", headers=VCI_HEADERS, params={"group": group}, timeout=20)
    codes = [str(item.get("symbol", "")).upper() for item in body or [] if isinstance(item, dict)]
    return [c for c in codes if c]
