"""Tiny stdlib HTTP helper with timeouts, retries and JSON decoding."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

USER_AGENT = "odds-watcher/1.0 (+https://github.com/loizos-stack/odds_watcher)"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


_SECRET_PATTERNS = (
    re.compile(r"(apiKey=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(api_key=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(/bot)[^/\s]+"),
)


def redact(text: str) -> str:
    """Strip API keys and bot tokens so they never reach a log or an exception."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1***", text)
    return text


class TransportError(RuntimeError):
    """Any failure while talking to a remote API (network, timeout, bad JSON)."""


class HttpError(TransportError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} for {redact(url)}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


class RateLimitedError(HttpError):
    """The upstream API answered 429 — we are over quota."""


def build_url(base: str, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", ())}
    if clean:
        url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
    return url


def request_json_with_headers(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Mapping[str, Any]] = None,
    timeout: int = 20,
    retries: int = 3,
    backoff: float = 2.0,
    sleep=time.sleep,
    headers: Optional[Mapping[str, str]] = None,
) -> tuple:
    """Like :func:`request_json`, but also returns the response headers.

    Providers that meter usage report the remaining allowance in a header, and
    that is the only way to see credit burn before the account runs dry.
    """
    return _request(
        url,
        method=method,
        payload=payload,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        sleep=sleep,
        headers=headers,
    )


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Mapping[str, Any]] = None,
    timeout: int = 20,
    retries: int = 3,
    backoff: float = 2.0,
    sleep=time.sleep,
    headers: Optional[Mapping[str, str]] = None,
) -> Any:
    """Perform a request and decode the JSON body."""
    data, _headers = _request(
        url,
        method=method,
        payload=payload,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        sleep=sleep,
        headers=headers,
    )
    return data


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Mapping[str, Any]] = None,
    timeout: int = 20,
    retries: int = 3,
    backoff: float = 2.0,
    sleep=time.sleep,
    headers: Optional[Mapping[str, str]] = None,
) -> tuple:
    """Perform a request and decode the JSON body.

    Retries transient failures (network errors and 429/5xx) with exponential
    backoff. A non-retryable status raises immediately so the caller can log a
    real error instead of hammering the API.
    """
    body = None
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
            return (json.loads(raw) if raw.strip() else None), response_headers
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - retry loop
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            error = (
                RateLimitedError(exc.code, raw, url)
                if exc.code == 429
                else HttpError(exc.code, raw, url)
            )
            if exc.code not in RETRYABLE_STATUS:
                raise error
            last_error = error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc

        if attempt < retries:
            delay = backoff ** attempt
            log.warning(
                "request failed (%s), retrying in %.0fs [%d/%d]",
                redact(str(last_error)),
                delay,
                attempt,
                retries,
            )
            sleep(delay)

    if isinstance(last_error, TransportError):
        raise last_error
    raise TransportError(f"{redact(url)} unreachable after {retries} attempts: {last_error}")
