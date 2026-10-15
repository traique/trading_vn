"""Download and update the research data set (incremental, resumable).

    python -m research download [--workers 4] [--refresh] [--limit N] [--symbols FPT,VNM]

The first run takes ~20 minutes and writes ~315 MB of CSV (~550 symbols; the
panel cache built on first use adds ~200 MB); later runs only fetch the
sessions after each file's last date. Every file is written to a temp file and
moved into place, so a killed run never leaves a half-written file. Ctrl-C
cancels the queued symbols, lets the running ones finish and saves
``data/download_meta.json``; the next run resumes where it stopped. Failures
are logged to ``data/download.log`` and listed in ``data/download_meta.json``
("failed"); rerun the command to retry them.

``data_end`` (the last session of the panel's calendar) moves forward only when
a full run finishes: ``--symbols`` / ``--limit`` and interrupted runs leave it
where it was, so the stocks they did not update never look suspended on the
newer sessions. ``--symbols`` also skips the monthly HOSE snapshots and the
delisted probe (steps 3 and 4 below), which only build the full symbol list.

What it fetches, all through the repo's own vendor modules
(``tradingagents.dataflows.vendors``):

1. VNINDEX and VN30 daily bars (``vci.fetch_ohlcv``, DNSE fallback).
2. The VCI listing (current boards, incl. the DELISTED stocks) and today's
   VN30 members (``money_flows.index_members``) - for comparison only.
3. Every HOSE stock on the first session of each month since 2013
   (``flows.fetch_hose_day``, VNDirect). Their union is the set of symbols
   that were on HOSE at some point - delisted ones included.
4. For each such symbol (plus today's HSX stocks and DELISTED stocks found to
   have been on HOSE in 2011-2012):
   - adjusted daily OHLCV 2011..today (``vci.fetch_ohlcv``; ``dnse.fetch_ohlcv``
     fallback) -> ``data/ohlcv/{SYM}.csv``. The repo's ``load_ohlcv`` keeps only
     5 years, so the vendor fetchers are called directly with a 2011 start.
     On an update the last ``OVERLAP_DAYS`` are fetched again: if their closes
     moved (a dividend or bonus issue re-adjusted the history) the whole
     history is fetched again.
   - VCI price-history (the endpoint ``flows.fetch_stock_flows`` reads):
     raw (unadjusted) close, reference/ceiling/floor, matched volume and
     value, shares outstanding -> ``data/ph/{SYM}.csv``. Used for the liquidity
     ranking (real VND traded value), the 5,000đ price floor (on the RAW
     price), the exchange the stock traded on each day (band width) and the
     market cap ranking of the defensive book (shares x raw close).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

from research.config import (
    DATA_DIR,
    HISTORY_START,
    LISTING_FILE,
    LOG_FILE,
    META_FILE,
    OHLCV_DIR,
    PH_DIR,
    PROBE_FILE,
    SNAP_FILE,
    VN30_FILE,
)

log = logging.getLogger("research.download")
INTERRUPTED = 130           # exit code after Ctrl-C (128 + SIGINT)

OVERLAP_DAYS = 20           # calendar days fetched again on an update (adjustment check)
ADJUST_TOLERANCE = 0.005    # relative close change on the overlap that means "re-adjusted"

PH_FIELDS = {
    "tradingDate": "Date", "openPrice": "RawOpen", "highestPrice": "RawHigh", "lowestPrice": "RawLow",
    "closePrice": "RawClose", "closePriceAdjusted": "AdjClose", "referencePrice": "Ref",
    "ceilingPrice": "Ceiling", "floorPrice": "Floor", "totalMatchVolume": "MatchVolume",
    "totalMatchValue": "MatchValue", "totalVolume": "TotalVolume", "totalValue": "TotalValue",
    "totalShares": "Shares", "marketCap": "MarketCap",
}


def _today_final() -> str:
    """The last session date whose bar is final (VN time >= 15:05), as YYYY-MM-DD."""
    from tradingagents.market import vn_rules

    now = vn_rules.now_vn()
    day = pd.Timestamp(now.date())
    if now.time() < datetime.strptime("15:05", "%H:%M").time():
        day -= pd.Timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def _write_csv(frame: pd.DataFrame, path) -> None:
    """Write ``path`` whole (temp file, then rename): a killed run leaves the old file or the new one."""
    from tradingagents.dataflows.files import replace_file

    replace_file(path, lambda tmp: frame.to_csv(tmp, index=False))


def _write_text(path, text: str) -> None:
    from tradingagents.dataflows.files import replace_file

    replace_file(path, lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))


def _read_csv(path) -> pd.DataFrame | None:
    """The file's rows, or None when it is missing, empty or unreadable (for
    instance cut short by an older version of this downloader) - fetched again."""
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):            # pandas' EmptyDataError / ParserError are ValueErrors
        return None
    return frame if "Date" in frame.columns else None


def _run_pool(fn, items, workers: int, handle) -> None:
    """``handle(item, future)`` for each ``fn(item)`` as it finishes, ``workers`` at a time.

    On Ctrl-C the queued tasks are cancelled (a plain ``with ThreadPoolExecutor``
    would run every one of them before the interrupt got through), the running
    ones finish and are handled, then KeyboardInterrupt is raised again.
    """
    pool = ThreadPoolExecutor(max_workers=workers)
    futs = {pool.submit(fn, x): x for x in items}
    handled = set()
    try:
        for fut in as_completed(futs):
            handle(futs[fut], fut)
            handled.add(fut)
    except KeyboardInterrupt:
        log.warning("interrupted: cancelling the queued tasks, waiting for the running ones")
        pool.shutdown(wait=True, cancel_futures=True)
        for fut, item in futs.items():
            if fut not in handled and fut.done() and not fut.cancelled():
                handle(item, fut)
        raise
    pool.shutdown(wait=True)


def _retry(fn, *args, tries: int = 3, pause: float = 3.0, **kw):
    from tradingagents.dataflows.errors import NoMarketDataError

    last = None
    for i in range(tries):
        try:
            return fn(*args, **kw)
        except NoMarketDataError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(pause * (i + 1))
    raise last


def fetch_bars(symbol: str, end: str, start: str = HISTORY_START) -> tuple[pd.DataFrame, str]:
    from tradingagents.dataflows.vendors import dnse, vci

    errors = []
    for name, fetch in (("vci", vci.fetch_ohlcv), ("dnse", dnse.fetch_ohlcv)):
        try:
            frame = _retry(fetch, symbol, start, end)
            frame = frame[frame["Date"] <= end]
            if not frame.empty:
                return frame.reset_index(drop=True), name
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    raise RuntimeError("; ".join(errors) or "no bars")


def fetch_price_history(symbol: str, start: str, end: str) -> pd.DataFrame:
    from tradingagents.dataflows.vendors import flows, http

    rows: list[dict] = []
    page = 0
    while True:
        body = _retry(http.get_json, f"{flows.VCI_COMPANY}/{symbol}/price-history", vendor="VCI",
                      headers=flows.VCI_HEADERS, timeout=40,
                      params={"timeFrame": "ONE_DAY", "page": page, "size": 1000,
                              "fromDate": start.replace("-", ""), "toDate": end.replace("-", "")})
        data = (body or {}).get("data") or {}
        content = data.get("content") or []
        rows.extend(content)
        pages = data.get("totalPages") or 1
        page += 1
        if not content or page >= pages:
            break
        time.sleep(0.3)
    frame = pd.DataFrame([{v: r.get(k) for k, v in PH_FIELDS.items()} for r in rows], columns=list(PH_FIELDS.values()))
    if frame.empty:
        return frame
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    frame = frame[frame["Date"] <= end]
    return frame.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)


def bars_from_price_history(ph: pd.DataFrame) -> pd.DataFrame:
    f = ph[(ph["RawClose"] > 0) & (ph["AdjClose"] > 0)].copy()
    k = f["AdjClose"] / f["RawClose"]
    out = pd.DataFrame({"Date": f["Date"]})
    for col, raw in (("Open", "RawOpen"), ("High", "RawHigh"), ("Low", "RawLow")):
        out[col] = (f[raw].where(f[raw] > 0, f["RawClose"]) * k).round(0)
    out["Close"] = f["AdjClose"].round(0)
    out["High"] = out[["High", "Open", "Close"]].max(axis=1)
    out["Low"] = out[["Low", "Open", "Close"]].min(axis=1)
    out["Volume"] = f["MatchVolume"].fillna(0)
    return out.reset_index(drop=True)


def fetch_vndirect(symbol: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    from tradingagents.dataflows.vendors import flows, http

    body = _retry(http.get_json, flows.VNDIRECT_PRICES, vendor="VNDirect", timeout=60,
                  params={"q": f"code:{symbol}", "size": 9000, "sort": "date"})
    rows = (body or {}).get("data") or []
    if not rows:
        raise RuntimeError(f"no data anywhere for {symbol}")
    f = pd.DataFrame(rows)
    f = f[(f["date"] <= end) & (f["close"] > 0)].drop_duplicates("date").sort_values("date")
    k = 1000.0
    bars = pd.DataFrame({"Date": f["date"], "Open": (f["adOpen"] * k).round(0), "High": (f["adHigh"] * k).round(0),
                         "Low": (f["adLow"] * k).round(0), "Close": (f["adClose"] * k).round(0),
                         "Volume": f["nmVolume"]})
    ex = f["floor"].map({"HOSE": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM"})
    ph = pd.DataFrame({"Date": f["date"], "RawOpen": f["open"] * k, "RawHigh": f["high"] * k, "RawLow": f["low"] * k,
                       "RawClose": f["close"] * k, "AdjClose": f["adClose"] * k, "Ref": f["basicPrice"] * k,
                       "Ceiling": f["ceilingPrice"] * k, "Floor": f["floorPrice"] * k,
                       "MatchVolume": f["nmVolume"], "MatchValue": f["nmValue"], "TotalVolume": f["nmVolume"] + f["ptVolume"],
                       "TotalValue": f["nmValue"] + f["ptValue"], "Shares": None, "MarketCap": None, "Exchange": ex})
    return bars.reset_index(drop=True), ph.reset_index(drop=True)


def monthly_sessions(vni: pd.DataFrame) -> list[str]:
    d = pd.to_datetime(vni["Date"])
    d = d[d >= "2013-01-01"]
    return sorted(d.groupby(d.dt.to_period("M")).min().dt.strftime("%Y-%m-%d"))


def fetch_snapshots(days: list[str], workers: int) -> tuple[pd.DataFrame, list[str]]:
    from tradingagents.dataflows.vendors import flows

    old = _read_csv(SNAP_FILE)
    if old is None:
        old = pd.DataFrame(columns=["Date", "Code", "Close", "AdClose"])
    have = set(old["Date"].astype(str))
    todo = [d for d in days if d not in have]
    out, failed = [old], []

    def one(day):
        f = _retry(flows.fetch_hose_day, day)
        f = f[["Code", "Close", "AdClose"]].copy()
        f.insert(0, "Date", day)
        return f

    def handle(day, fut):
        try:
            f = fut.result()
            if f.empty:
                failed.append(day)
            out.append(f)
        except Exception as exc:  # noqa: BLE001
            failed.append(day)
            log.warning("snapshot %s failed: %s", day, exc)

    try:
        _run_pool(one, todo, min(workers, 2), handle)
    finally:                                  # keep the months fetched so far, even on Ctrl-C
        snap = pd.concat(out, ignore_index=True).drop_duplicates(["Date", "Code"]).sort_values(["Date", "Code"])
        _write_csv(snap, SNAP_FILE)
    return snap, failed


def infer_exchange(ph: pd.DataFrame) -> pd.Series:
    """The board a stock traded on each day, from its band (ceiling / reference).

    Bands: HOSE 7% / HNX 10% / UPCoM 15% from 15/01/2013; 5/7/10% before.
    A 20-session rolling median absorbs first-day and ex-date bands.
    """
    band = (ph["Ceiling"] / ph["Ref"] - 1) * 100
    band = band.where((band > 0) & (band < 50)).rolling(20, min_periods=1).median().ffill().bfill()
    new = pd.to_datetime(ph["Date"]) >= "2013-01-15"
    hose = band.where(new, band + 3.2) < 8.5      # before 2013 only a <5.3% band reads as HOSE
    hnx = band.where(new, band + 3) < 12.5
    out = pd.Series("UPCOM", index=ph.index)
    out[hnx] = "HNX"
    out[hose] = "HOSE"
    out[band.isna()] = None
    return out


def _last_date(path) -> str | None:
    try:
        dates = pd.read_csv(path, usecols=["Date"])["Date"].astype(str)
    except (ValueError, OSError):
        return None
    return dates.max() if len(dates) else None


def _has_header(path) -> bool:
    try:
        return "Date" in pd.read_csv(path, nrows=0).columns
    except (ValueError, OSError):
        return False


def _overlap_start(last: str) -> str:
    return (pd.Timestamp(last) - pd.Timedelta(days=OVERLAP_DAYS)).strftime("%Y-%m-%d")


def update_price_history(symbol: str, pfile, end: str) -> pd.DataFrame:
    """Append the sessions after the file's last date (raw prices never re-adjust)."""
    old = _read_csv(pfile)
    last = str(old["Date"].max()) if old is not None and len(old) else None
    if last is None:
        return fetch_price_history(symbol, HISTORY_START, end)
    if last >= end:
        return old
    new = fetch_price_history(symbol, _overlap_start(last), end)
    if new.empty:
        return old
    old = old.drop(columns=["Exchange"], errors="ignore")
    merged = pd.concat([old[old["Date"].astype(str) < new["Date"].min()], new], ignore_index=True)
    return merged.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)


def update_bars(symbol: str, ofile, end: str) -> tuple[pd.DataFrame | None, str]:
    """New bars after the file's last date, or the whole history again when the
    overlap shows the vendor re-adjusted it. (None, ...) when already up to date."""
    old = _read_csv(ofile)
    last = str(old["Date"].max()) if old is not None and len(old) else None
    if last is None:
        return fetch_bars(symbol, end)
    if last >= end:
        return None, "up to date"
    new, source = fetch_bars(symbol, end, start=_overlap_start(last))
    new = new.assign(Date=pd.to_datetime(new["Date"]).dt.strftime("%Y-%m-%d"))
    both = old.assign(Date=old["Date"].astype(str)).merge(new, on="Date", suffixes=("_old", ""))
    moved = (both["Close"] / both["Close_old"] - 1).abs() > ADJUST_TOLERANCE
    if both.empty or moved.any():
        return fetch_bars(symbol, end)[0], f"{source} (re-adjusted: full history)"
    merged = pd.concat([old[old["Date"].astype(str) < new["Date"].min()], new], ignore_index=True)
    return merged.drop_duplicates("Date", keep="last").reset_index(drop=True), source


def download_symbol(symbol: str, end: str, refresh: bool, frozen: bool = False) -> dict:
    """Fetch or update one symbol's files. ``frozen``: a delisted stock whose files
    exist (nothing new to fetch). The files are written only when the symbol
    succeeds, so a failed symbol (a typo, a vendor outage) leaves nothing behind."""
    ofile, pfile = OHLCV_DIR / f"{symbol}.csv", PH_DIR / f"{symbol}.csv"
    info = {"symbol": symbol}
    try:
        ph = None
        bars = None
        is_index = symbol in ("VNINDEX", "VN30")
        if not is_index:
            if refresh or not _has_header(pfile):
                ph = fetch_price_history(symbol, HISTORY_START, end)
            elif not frozen:
                ph = update_price_history(symbol, pfile, end)
            if ph is not None and not ph.empty and "Ceiling" in ph:
                ph["Exchange"] = infer_exchange(ph)
        new_ph = ph
        if refresh or _last_date(ofile) is None:
            try:
                bars, source = fetch_bars(symbol, end)
            except RuntimeError:
                # Some delisted stocks have no chart bars any more: rebuild adjusted bars
                # from VCI price-history (raw OHLC x its adjusted/raw close ratio).
                if ph is None:
                    ph = _read_csv(pfile)
                if ph is None or ph.empty:
                    # Last resort (delisted stocks VCI no longer serves): VNDirect, from 2013.
                    bars, ph = fetch_vndirect(symbol, end)
                    new_ph = ph
                    source = "vndirect"
                else:
                    bars, source = bars_from_price_history(ph), "vci-price-history"
            info["bars_source"] = source
        elif not frozen:
            try:
                bars, source = update_bars(symbol, ofile, end)
            except RuntimeError:
                if ph is None or ph.empty or "AdjClose" not in ph:
                    raise
                bars, source = bars_from_price_history(ph), "vci-price-history"
            info["update"] = source
        if new_ph is not None:
            _write_csv(new_ph, pfile)
        if bars is not None:
            _write_csv(bars, ofile)
        info["last_date"] = _last_date(ofile)
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001
        info["ok"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("%s failed: %s", symbol, info["error"])
    return info


MAX_WORKERS = 8             # be polite to the vendors


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(getattr(h, "_research", False) for h in root.handlers):
        for handler in (logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")):
            handler.setFormatter(fmt)
            handler._research = True
            root.addHandler(handler)
    logging.getLogger("tradingagents").setLevel(logging.ERROR)


def build_parser(ap: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    ap = ap or argparse.ArgumentParser(prog="python -m research download", description=__doc__.split("\n")[0])
    ap.add_argument("--workers", type=int, default=4, help=f"parallel requests (max {MAX_WORKERS}, default 4)")
    ap.add_argument("--refresh", action="store_true", help="fetch every file again from 2011")
    ap.add_argument("--limit", type=int, default=0, help="only the first N symbols (testing)")
    ap.add_argument("--symbols", default="", help="only these symbols, comma-separated (e.g. FPT,VNM)")
    return ap


def _wanted(args) -> list[str]:
    return sorted({s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()})


def _symbol_list(args, workers: int, vni: pd.DataFrame, stocks: dict, vn30: list[str], meta: dict) -> list[str]:
    """Every stock that was on HOSE at some point (steps 3-4), or the ``--symbols`` given."""
    wanted = _wanted(args)
    if wanted:
        log.info("--symbols: skipping the monthly HOSE snapshots and the delisted probe")
        return wanted
    snap, snap_failed = fetch_snapshots(monthly_sessions(vni), workers)
    meta["snapshot_failed"] = snap_failed
    log.info("snapshots: %d rows, %d months, %d failed", len(snap), snap["Date"].nunique(), len(snap_failed))

    hose_ever = set(snap["Code"].astype(str)) | {s for s, x in stocks.items() if x.get("board") == "HSX"} | set(vn30)
    hose_ever = {s for s in hose_ever if len(s) == 3 and s.isalnum()}
    # Stocks delisted before 2013 never appear in the snapshots: probe 2011-2012.
    probe = json.loads(PROBE_FILE.read_text()) if PROBE_FILE.exists() else {}
    extra = [s for s, x in stocks.items() if x.get("board") in ("DELISTED",) and s not in hose_ever
             and probe.get(s) is None]

    def probe_one(s):
        try:
            ph = fetch_price_history(s, "2011-01-01", "2012-12-31")
            if ph.empty:
                return False
            return bool((infer_exchange(ph) == "HOSE").any())
        except Exception:  # noqa: BLE001
            return None

    def handle(s, fut):
        probe[s] = fut.result()

    try:
        _run_pool(probe_one, extra, workers, handle)
    finally:
        _write_text(PROBE_FILE, json.dumps(probe))
    hose_ever |= {s for s, v in probe.items() if v}
    symbols = sorted(hose_ever)
    return symbols[: args.limit] if args.limit else symbols


def _record(meta: dict, results: dict, done: dict) -> tuple[list[str], list[str]]:
    """Store the results in ``meta``; (failed in this run, still failed from earlier runs).

    A failure of an earlier run is kept while the symbol has bars on disk (they
    are stale) and forgotten otherwise (a typo given to ``--symbols``, say):
    a run that does not try a symbol again cannot keep reporting it.
    """
    for s in [s for s, v in results.items() if s not in done and not v.get("ok")]:
        if _last_date(OHLCV_DIR / f"{s}.csv") is None:
            del results[s]
    meta["symbols"] = results
    meta["failed"] = sorted(s for s, v in results.items() if not v.get("ok"))
    now = sorted(s for s, v in done.items() if not v.get("ok"))
    return now, sorted(set(meta["failed"]) - set(now))


def main(argv=None, args=None) -> int:
    args = args or build_parser().parse_args(argv)
    workers = max(1, min(int(args.workers), MAX_WORKERS))
    for d in (DATA_DIR, OHLCV_DIR, PH_DIR):
        d.mkdir(parents=True, exist_ok=True)
    _setup_logging()
    end = _today_final()
    meta = json.loads(META_FILE.read_text()) if META_FILE.exists() else {}
    prev_end = meta.get("data_end")
    partial = bool(_wanted(args) or args.limit)
    log.info("data directory %s; sessions up to %s (previous data end %s)", DATA_DIR, end, prev_end)
    meta.update(started=datetime.now().isoformat(timespec="seconds"))
    results = meta.get("symbols", {})
    done: dict[str, dict] = {}

    def save():
        meta["symbols"] = results
        _write_text(META_FILE, json.dumps(meta, indent=1))

    try:
        for idx in ("VNINDEX", "VN30"):
            info = download_symbol(idx, end, True)
            log.info("%s: %s", idx, info)
            if not info.get("ok") and _last_date(OHLCV_DIR / f"{idx}.csv") is None:
                log.error("no %s bars: stopping (rerun later)", idx)
                return 1
        vni = pd.read_csv(OHLCV_DIR / "VNINDEX.csv")

        from tradingagents.dataflows import money_flows
        from tradingagents.dataflows.vendors import vci

        listing = _retry(vci.fetch_listing)
        _write_text(LISTING_FILE, json.dumps(listing, ensure_ascii=False))
        vn30 = _retry(money_flows.index_members, "VN30")
        _write_text(VN30_FILE, json.dumps({"as_of": end, "codes": vn30}))
        log.info("listing %d codes, VN30 today %d", len(listing), len(vn30))

        stocks = {x["symbol"]: x for x in listing if x.get("type") == "STOCK"}
        symbols = _symbol_list(args, workers, vni, stocks, vn30, meta)
        delisted = {s for s, x in stocks.items() if x.get("board") == "DELISTED"}
        log.info("downloading %d symbols with %d workers", len(symbols), workers)

        t0 = time.time()

        def task(s):
            ofile, pfile = OHLCV_DIR / f"{s}.csv", PH_DIR / f"{s}.csv"
            frozen = s in delisted and _last_date(ofile) is not None and _has_header(pfile)
            return download_symbol(s, end, args.refresh, frozen)

        def handle(_s, fut):
            info = fut.result()
            prev = results.get(info["symbol"], {})
            if info.get("ok"):
                prev.pop("error", None)
            results[info["symbol"]] = {**prev, **info}
            done[info["symbol"]] = info
            if len(done) % 25 == 0:
                log.info("%d/%d done (%.0fs)", len(done), len(symbols), time.time() - t0)
                save()

        _run_pool(task, symbols, workers, handle)
    except KeyboardInterrupt:
        _record(meta, results, done)
        meta["interrupted"] = datetime.now().isoformat(timespec="seconds")
        save()
        log.warning("interrupted after %d symbols; finished files are kept, data end stays %s. "
                    "Rerun `python -m research download` to continue.", len(done), meta.get("data_end"))
        return INTERRUPTED

    # The calendar moves only when every symbol was brought up to ``end``: after a
    # partial run, stocks it did not update would show NaN (a suspension) on the new sessions.
    if not partial or prev_end is None:
        meta["data_end"] = end
    elif end > prev_end:
        log.info("partial run: data end stays %s (run without --symbols/--limit to move every symbol to %s)",
                 prev_end, end)
    failed_now, stale = _record(meta, results, done)
    meta.pop("interrupted", None)
    meta["finished"] = datetime.now().isoformat(timespec="seconds")
    save()
    log.info("done: %d ok, %d failed: %s", len(done) - len(failed_now), len(failed_now), failed_now[:30])
    if stale:
        log.info("still failed from earlier runs (bars not updated): %s", stale[:30])
    if meta["failed"]:
        log.info("rerun `python -m research download` to retry the failed symbols (until then they have no "
                 "bars after their last good download and look suspended up to %s)", meta.get("data_end"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
