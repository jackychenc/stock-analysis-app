"""Shared adapter plumbing — extracted from the task-#8 yfinance adapter so
task #9's chip adapters (TWSE/TPEx + EDGAR 13F) inherit the SAME reviewed
controls instead of re-implementing them:
- AdapterUnavailable (per-source honesty: no partial silent success);
- _with_retries: bounded retries, exponential backoff + jitter (A8 #3);
- _num/_int: decimal-safe converters — NaN/inf/garbage -> None (A8 #4);
- SYMBOL_RE + check_symbol: fullmatch egress allowlist (A8 #1, Y-1 lesson);
- http_get: hardened stdlib urllib GET — HTTPS-only, pinned host allowlist,
  redirect refusal outside the allowlist, timeout, response-size cap (A8 #2/#3).
"""

import asyncio
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# A8 #3 (mandatory after A7's live 429 pre-flight): explicit outbound timeout,
# paced requests, bounded retries w/ exponential backoff + jitter — no storms.
REQUEST_TIMEOUT_S = 15
PACING_DELAY_S = 2.0  # pause between per-ticker fetches — polite egress
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 1.0
# DNS resolution failures are transient per A7's ruling (bounded retry);
# the allowlist is re-asserted per hostname on every attempt, never by IP.
_TRANSIENT_MARKERS = ("429", "too many requests", "rate limit",
                      "name or service not known", "nodename nor servname",
                      "temporary failure in name resolution", "getaddrinfo",
                      "500", "502", "503", "504", "timed out", "timeout")

# A8 #1: allowlist at the adapter boundary — symbols feed outbound URL
# construction, so a bad ticker row must never shape an egress request.
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")


class AdapterUnavailable(Exception):
    """Raised when the source produced no usable data at all — the pipeline
    marks the whole source 'unavailable' (never a partial silent success)."""


def check_symbol(symbol: str) -> None:
    """A8 #1: allowlist before any egress. fullmatch (Y-1): `$` would admit a
    trailing newline — this regex IS the SSRF boundary, so match exactly."""
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol rejected by egress allowlist")


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


async def _with_retries(fn, /, *args, sleeper=asyncio.sleep, rng=None) -> Any:
    """Run blocking client call in a worker thread; retry transient failures
    (429/5xx/timeouts) with exponential backoff + jitter. Non-transient errors
    raise immediately (per-ticker isolation handles them). Exhausted retries
    re-raise the transient error — the source then reads 'unavailable', never
    a tight-loop hammer (R-01)."""
    import random

    rng = rng or random.random
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS or not _is_transient(exc):
                raise
            logger.warning("transient upstream error (attempt %d/%d): %s",
                           attempt, RETRY_ATTEMPTS, exc)
            base = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            await sleeper(base * (1 + rng() * 0.5))  # jitter: 1.0x..1.5x


def _num(value: Any) -> Decimal | None:
    """Decimal-safe conversion; NaN/None/garbage -> None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return Decimal(str(value)) if not isinstance(value, float) else Decimal(repr(f))


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f)


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A8 #2 egress pin: a redirect that leaves the source's host allowlist
    (or downgrades to http) is refused — an upstream compromise must not be
    able to bounce us to an attacker host."""

    def __init__(self, allowed_hosts: frozenset[str]):
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlparse(newurl)
        if parts.scheme != "https" or parts.hostname not in self._allowed_hosts:
            raise urllib.error.URLError(
                f"redirect refused: target outside egress allowlist ({parts.hostname})"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    max_bytes: int,
    headers: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
) -> bytes:
    """Hardened GET (stdlib urllib — httpx is dev-group only): HTTPS-only,
    host pinned to the allowlist BEFORE egress, redirects refused outside it,
    explicit timeout, response read capped at max_bytes (A8 #2/#3/#4)."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "https":
        raise ValueError("egress must be https")
    if parts.hostname not in allowed_hosts:
        raise ValueError(f"host not in egress allowlist: {parts.hostname}")
    opener = urllib.request.build_opener(_PinnedRedirectHandler(allowed_hosts))
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"response exceeds size cap ({max_bytes} bytes)")
    return body
