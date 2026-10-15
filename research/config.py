"""Paths and the fixed research splits.

The data directory defaults to ``research/data`` next to this file and can be
moved with the environment variable ``TRADING_VN_RESEARCH_DATA`` (read at
import: set it before importing ``research``). It is never committed
(``.gitignore``); ``python -m research download`` fills it.
"""

from __future__ import annotations

import os
from pathlib import Path

RESEARCH_DIR = Path(__file__).resolve().parent
DATA_ENV = "TRADING_VN_RESEARCH_DATA"
DATA_DIR = Path(os.environ.get(DATA_ENV) or RESEARCH_DIR / "data").expanduser().resolve()
OHLCV_DIR = DATA_DIR / "ohlcv"          # adjusted daily bars (repo vendors: VCI gap-chart, DNSE fallback)
PH_DIR = DATA_DIR / "ph"                # VCI price-history: raw prices, matched value, ceiling/floor, shares
SNAP_FILE = DATA_DIR / "hose_monthly.csv"   # VNDirect: every HOSE stock on the first session of each month
META_FILE = DATA_DIR / "download_meta.json"
LISTING_FILE = DATA_DIR / "listing.json"
VN30_FILE = DATA_DIR / "vn30_today.json"
PROBE_FILE = DATA_DIR / "delisted_probe.json"
LOG_FILE = DATA_DIR / "download.log"
# Pickle cache of the built panel. Not ``panel.pkl``: a data directory made by
# the original research harness keeps its own cache (a different class path).
PANEL_CACHE = DATA_DIR / "research_panel.pkl"
# Default output directory of ``python -m research baselines`` (ignored by git).
OUT_DIR = RESEARCH_DIR / "out"

HISTORY_START = "2011-01-01"

# Fixed splits. Tune on TRAIN, check on VALID; TEST is run once, at the end.
TRAIN = ("2013-01-01", "2020-12-31")
VALID = ("2021-01-01", "2023-12-31")
TEST = ("2024-01-01", "2099-12-31")
TRAIN_VALID = (TRAIN[0], VALID[1])
ALL = (TRAIN[0], TEST[1])
LOCK_AFTER = VALID[1]       # run() refuses an end after this unless unlock_test=True

SPLITS = {"TRAIN": TRAIN, "VALID": VALID, "TRAIN+VALID": TRAIN_VALID}

INDEXES = ("VNINDEX", "VN30")


def data_ready() -> bool:
    """Whether the data directory holds a downloaded data set."""
    return (OHLCV_DIR / "VNINDEX.csv").exists()
