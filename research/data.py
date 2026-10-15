"""The cached data set as aligned wide panels (dates x symbols).

``load_panel()`` returns a ``Panel``; download the data first with
``python -m research download``.
Every panel is indexed by the VNINDEX session calendar (2011..data end); a
cell is NaN on a session the stock had no bar (not listed yet, suspended,
delisted).

Fields
- open/high/low/close/volume: adjusted daily bars (VCI gap-chart, as the repo's
  loaders give them). volume is in shares as traded (not adjusted).
- raw_close: unadjusted close (VCI price-history) - for price floors.
- value: matched traded value in VND (VCI price-history ``totalMatchValue``,
  close*volume as a fallback where missing) - for liquidity.
- exchange: the board the stock traded on that session (inferred from the
  daily band; "HOSE" / "HNX" / "UPCOM").
- vnindex / vn30: index closes (Series), plus ``vnindex_bars`` (OHLCV frame).
- price_history: {symbol: frame} of the VCI price-history rows as the vendor
  gives them (Date index; MatchValue, RawClose, Shares; NaN where missing),
  for the live defensive book's universe (``research.factor``).
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research.config import (
    LOCK_AFTER,
    META_FILE,
    OHLCV_DIR,
    PANEL_CACHE,
    PH_DIR,
    SNAP_FILE,
    VN30_FILE,
)
from research.download import infer_exchange

log = logging.getLogger("research.data")

FIELDS = ("open", "high", "low", "close", "volume", "raw_close", "value")
PH_COLUMNS = ("MatchValue", "RawClose", "Shares")
CACHE_VERSION = 2           # bump when the panel's layout changes (rebuilds older caches)


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    raw_close: pd.DataFrame
    value: pd.DataFrame
    exchange: pd.DataFrame
    vnindex: pd.Series
    vn30: pd.Series
    vnindex_bars: pd.DataFrame
    vn30_today: list[str]
    info: dict = field(default_factory=dict)
    price_history: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    def date_index(self, day) -> int:
        """Index of the last session on or before ``day`` (-1 when none)."""
        return int(self.dates.searchsorted(pd.Timestamp(day), side="right")) - 1


def _read_bars(path) -> pd.DataFrame:
    f = pd.read_csv(path)
    f["Date"] = pd.to_datetime(f["Date"])
    f = f.dropna(subset=["Close"])
    f = f[f["Close"] > 0].drop_duplicates("Date", keep="last").set_index("Date").sort_index()
    return f


def _apply_snapshots(ex: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """HOSE membership from VNDirect's monthly snapshots (exact board field).

    From 2013: a stock is "HOSE" for every session of month M iff it is in the
    snapshot of M's first session (a mid-month move shows from the next month);
    otherwise its inferred board, never "HOSE" (low-priced HNX/UPCoM stocks can
    look like 7% bands after tick rounding). Before the first snapshot
    (2011-2012, bands 5/7/10%): members of the first snapshot are HOSE; other
    stocks keep the band inference.
    """
    if not SNAP_FILE.exists():
        return ex
    snap = pd.read_csv(SNAP_FILE)
    snap["Date"] = pd.to_datetime(snap["Date"])
    members = snap.groupby("Date")["Code"].apply(set)
    out = ex.copy()
    non_hose = ex.where(ex != "HOSE", None)
    first = members.index.min()
    cols = list(ex.columns)
    for k, (day, codes) in enumerate(members.items()):
        nxt = members.index[k + 1] if k + 1 < len(members) else dates[-1] + pd.Timedelta(days=1)
        rows = (dates >= day) & (dates < nxt)
        mask = pd.Series([c in codes for c in cols], index=cols)
        block = non_hose.loc[rows].copy()
        block.loc[:, mask.to_numpy()] = "HOSE"
        out.loc[rows] = block
    early = dates < first
    codes0 = members.iloc[0]
    for c in cols:
        if c in codes0:
            out.loc[early, c] = "HOSE"
    return out


def build_panel() -> Panel:
    vni = _read_bars(OHLCV_DIR / "VNINDEX.csv")
    vn30 = _read_bars(OHLCV_DIR / "VN30.csv")
    meta = json.loads(META_FILE.read_text()) if META_FILE.exists() else {}
    end = pd.Timestamp(meta.get("data_end", vni.index.max()))
    vni = vni[vni.index <= end]
    dates = vni.index
    cols = {k: {} for k in FIELDS}
    exch = {}
    history = {}
    for path in sorted(OHLCV_DIR.glob("*.csv")):
        sym = path.stem
        if sym in ("VNINDEX", "VN30"):
            continue
        try:
            bars = _read_bars(path)
        except (ValueError, KeyError, OSError) as exc:    # empty / garbled file: skip the stock, say so
            log.warning("skipping %s: unreadable bars (%s); rerun `python -m research download`", path.name, exc)
            continue
        bars = bars[bars.index <= end]
        if bars.empty:
            continue
        bars = bars[bars.index.isin(dates)]      # sessions off the market calendar are vendor noise
        cols["open"][sym] = bars["Open"]
        cols["high"][sym] = bars["High"]
        cols["low"][sym] = bars["Low"]
        cols["close"][sym] = bars["Close"]
        cols["volume"][sym] = bars["Volume"]
        php = PH_DIR / f"{sym}.csv"
        ph = None
        if php.exists():
            try:
                ph = pd.read_csv(php)
            except (ValueError, OSError):
                ph = None
        if ph is not None and not ph.empty:
            ph["Date"] = pd.to_datetime(ph["Date"])
            ph = ph.drop_duplicates("Date").set_index("Date").sort_index()
            ph = ph[ph.index <= end]
            history[sym] = ph.reindex(columns=list(PH_COLUMNS)).astype(float)
            raw = ph["RawClose"].where(ph["RawClose"] > 0)
            val = ph["MatchValue"].where(ph["MatchValue"] >= 0)
            cols["raw_close"][sym] = raw
            cols["value"][sym] = val.reindex(bars.index).fillna(bars["Close"] * bars["Volume"]).combine_first(val)
            inferred = infer_exchange(ph.reset_index())
            inferred.index = ph.index
            exch[sym] = inferred.where(inferred.notna(), ph.get("Exchange"))
        else:
            cols["raw_close"][sym] = pd.Series(np.nan, index=bars.index)
            cols["value"][sym] = bars["Close"] * bars["Volume"]
            exch[sym] = pd.Series(None, index=bars.index, dtype=object)

    def wide(d):
        return pd.DataFrame(d).reindex(dates).sort_index(axis=1).astype(float)

    frames = {k: wide(v) for k, v in cols.items()}
    # Only bars count: values on sessions without a bar are dropped.
    has_bar = frames["close"].notna()
    for k in ("raw_close", "value"):
        frames[k] = frames[k].where(has_bar)
    ex = pd.DataFrame(exch).reindex(dates).sort_index(axis=1).ffill()
    ex = ex.reindex(columns=frames["close"].columns)
    ex = _apply_snapshots(ex, dates)
    vn30_today = []
    if VN30_FILE.exists():
        vn30_today = json.loads(VN30_FILE.read_text())["codes"]
    return Panel(
        dates=dates, exchange=ex, vnindex=vni["Close"], vn30=vn30["Close"].reindex(dates),
        vnindex_bars=vni, vn30_today=sorted(vn30_today),
        info={"data_end": str(end.date()), "n_symbols": frames["close"].shape[1], "version": CACHE_VERSION},
        price_history=history, **frames,
    )


_FULL: Panel | None = None
_LOCKED: Panel | None = None


def truncate(panel: Panel, end) -> Panel:
    """A copy of ``panel`` holding no session after ``end``."""
    n = int(panel.dates.searchsorted(pd.Timestamp(end), side="right"))
    kw = {k: getattr(panel, k).iloc[:n] for k in (*FIELDS, "exchange", "vnindex", "vn30")}
    cut = pd.Timestamp(end)
    bars = panel.vnindex_bars[panel.vnindex_bars.index <= cut]
    history = {s: f[f.index <= cut] for s, f in panel.price_history.items()}
    return Panel(dates=panel.dates[:n], vnindex_bars=bars, vn30_today=list(panel.vn30_today),
                 info={**panel.info, "truncated_at": str(cut.date())}, price_history=history, **kw)


def _cache_fresh() -> bool:
    if not PANEL_CACHE.exists():
        return False
    return not META_FILE.exists() or PANEL_CACHE.stat().st_mtime >= META_FILE.stat().st_mtime


def load_panel(rebuild: bool = False, *, unlock_test: bool = False) -> Panel:
    """The panel (pickle-cached). Without ``unlock_test`` the panel returned
    ends on the last VALID session (2023-12-31).

    That is a copy: the full panel, TEST sessions included, stays in this
    module (``_FULL``) and in the pickle cache. The lock is ``run()``'s end-date
    check plus strategies seeing only the truncated panel, not the data being
    absent - research code must not reach for ``_FULL`` or the cache file.
    """
    global _FULL, _LOCKED
    if _FULL is None or rebuild:
        _FULL = None
        if not rebuild and _cache_fresh():
            with open(PANEL_CACHE, "rb") as fh:
                cached = pickle.load(fh)
            if isinstance(cached, Panel) and cached.info.get("version") == CACHE_VERSION:
                _FULL = cached
        if _FULL is None:
            if not (OHLCV_DIR / "VNINDEX.csv").exists():
                raise FileNotFoundError(f"no research data in {OHLCV_DIR.parent}: run `python -m research download` "
                                        "(or point TRADING_VN_RESEARCH_DATA at a downloaded data directory)")
            _FULL = build_panel()
            with open(PANEL_CACHE, "wb") as fh:
                pickle.dump(_FULL, fh, protocol=pickle.HIGHEST_PROTOCOL)
        _LOCKED = None
    if unlock_test:
        return _FULL
    if _LOCKED is None:
        _LOCKED = truncate(_FULL, LOCK_AFTER)
    return _LOCKED
