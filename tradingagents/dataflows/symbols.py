"""Vietnamese ticker normalization, listing lookups, and ticker values safe in a path.

Users and models write the same instrument several ways; every data path
resolves it here first:

    user types                 canonical
    ------------------------   ---------
    fpt, FPT, FPT.VN, HOSE:FPT FPT
    VN-Index, ^VNINDEX, VNI    VNINDEX
    HNXINDEX, HNX-Index        HNX
    UPCOMINDEX                 UPCOM

The listing (every code with its board, name and ICB code) comes from VCI and
is cached for a day; ``exchange_of`` and ``listing_entry`` read it.
"""

from __future__ import annotations

import logging
import re

from tradingagents.market import vn_rules

logger = logging.getLogger(__name__)

_PREFIX_RE = re.compile(r"^(?:HOSE|HSX|HNX|UPCOM|VN)\s*[:/]\s*")
_SUFFIX_RE = re.compile(r"\.(?:VN|HM|HN|HNO|VNM|HOSE|HSX|HNX|UPCOM)$")
_CODE_RE = re.compile(r"^[A-Z0-9]{1,10}$")


def normalize_symbol(raw: str) -> str:
    """The canonical code of a Vietnamese stock or index. Purely syntactic."""
    if not isinstance(raw, str) or not raw.strip():
        return raw
    s = raw.strip().upper().replace(" ", "")
    s = _PREFIX_RE.sub("", s)
    s = _SUFFIX_RE.sub("", s)
    canonical = vn_rules.INDEX_ALIASES.get(s, s)
    if canonical != raw.strip().upper():
        logger.debug("Resolved symbol %r to %r", raw, canonical)
    return canonical


def is_valid_code(symbol: str) -> bool:
    """Whether ``symbol`` has the shape of a Vietnamese code (letters/digits, ≤10)."""
    return bool(_CODE_RE.fullmatch(normalize_symbol(symbol) or ""))


def listing_entry(symbol: str) -> dict | None:
    """The VCI listing row of a code (``board``, ``organName``, ``icbCode2``...), or None.

    Indices are not in the listing. A listing that cannot be fetched returns
    None rather than raising: callers treat the exchange as unknown.
    """
    from tradingagents.dataflows.vendors import vci

    code = normalize_symbol(symbol)
    try:
        rows = vci.fetch_listing()
    except Exception as exc:  # noqa: BLE001 - listing is enrichment, never fatal
        logger.warning("VCI listing unavailable: %s", exc)
        return None
    for row in rows:
        if str(row.get("symbol", "")).upper() == code:
            return row
    return None


def exchange_of(symbol: str) -> str | None:
    """HOSE / HNX / UPCOM for a listed stock, else None (indices, unknown codes)."""
    if vn_rules.is_index(symbol):
        return None
    entry = listing_entry(symbol)
    return vn_rules.normalize_exchange(entry.get("board")) if entry else None


# Tickers can contain letters, digits, dot, dash, underscore and caret. None of
# these enable directory traversal, so the value never escapes a containing
# directory when interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value
