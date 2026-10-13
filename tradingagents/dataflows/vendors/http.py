"""HTTP access shared by the Vietnamese vendors (DNSE, VCI, CafeF, Google News).

Every endpoint here is public and keyless. A failed request says nothing about
the instrument, so transport errors, throttling and server errors raise
``VendorUnavailableError`` for the router to move on to the next vendor.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 10.0
_RETRY_STATUSES = {429, 500, 502, 503, 504}

_local = threading.local()


def _session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"})
        _local.session = session
    return session


def request(
    method: str,
    url: str,
    *,
    vendor: str,
    params: dict | None = None,
    json: dict | None = None,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    passthrough: tuple[int, ...] = (),
) -> requests.Response:
    """One request with a short retry on throttling, server errors and timeouts.

    Statuses in ``passthrough`` are returned for the caller to read (DNSE, for
    instance, answers an unknown index with HTTP 400).
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = _session().request(
                method, url, params=params, json=json, headers=headers, timeout=timeout
            )
        except requests.RequestException as exc:
            last_error = exc
        else:
            if response.status_code in passthrough or response.ok:
                return response
            last_error = requests.HTTPError(f"HTTP {response.status_code} from {vendor}")
            if response.status_code not in _RETRY_STATUSES:
                break
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    raise VendorUnavailableError(f"{vendor} request failed: {last_error}")


def get_json(url: str, *, vendor: str, **kwargs):
    response = request("GET", url, vendor=vendor, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise VendorUnavailableError(f"{vendor} returned a non-JSON body") from exc


def post_json(url: str, *, vendor: str, **kwargs):
    response = request("POST", url, vendor=vendor, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise VendorUnavailableError(f"{vendor} returned a non-JSON body") from exc


def get_text(url: str, *, vendor: str, **kwargs) -> str:
    return request("GET", url, vendor=vendor, **kwargs).text
