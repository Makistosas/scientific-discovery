"""Polite, cached HTTP access shared by every data-source client.

* SQLite response cache with a TTL, so repeated runs (and the continuous
  monitor) do not hammer public APIs.
* Per-host rate limiting (NCBI allows 3 req/s without a key, bioRxiv is slow…).
* Retries with exponential back-off on 429/5xx and network errors, honouring
  ``Retry-After``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .config import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

# Minimum seconds between two requests to the same host.
HOST_MIN_INTERVAL = {
    "eutils.ncbi.nlm.nih.gov": 0.34,
    "api.biorxiv.org": 0.5,
    "www.ebi.ac.uk": 0.1,
    "rest.uniprot.org": 0.1,
    "clinicaltrials.gov": 0.2,
    "search.rcsb.org": 0.1,
    "data.rcsb.org": 0.1,
    "api.platform.opentargets.org": 0.05,
}


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class ResponseCache:
    """Tiny thread-safe SQLite key/value cache for JSON responses."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, created REAL NOT NULL, body TEXT NOT NULL)"
        )
        self._conn.commit()

    def get(self, key: str, ttl_seconds: float) -> Any | None:
        with self._lock:
            row = self._conn.execute("SELECT created, body FROM cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        created, body = row
        if ttl_seconds >= 0 and time.time() - created > ttl_seconds:
            return None
        return json.loads(body)

    def set(self, key: str, value: Any) -> None:
        body = json.dumps(value)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, created, body) VALUES (?, ?, ?)",
                (key, time.time(), body),
            )
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cache")
            self._conn.commit()


class _HostThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str, interval: float) -> None:
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


class HttpClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: ResponseCache | None | bool = True,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings or Settings.from_env()
        if cache is True:
            cache = ResponseCache(self.settings.cache_dir / "http_cache.sqlite")
        self.cache: ResponseCache | None = cache or None
        self._session_factory = session_factory
        self._local = threading.local()
        self._throttle = _HostThrottle()
        self._sleep = sleep

    # -- public API -----------------------------------------------------
    def get_json(self, url: str, params: dict | None = None, *, ttl_hours: float | None = None,
                 headers: dict | None = None) -> Any:
        return self._request("GET", url, params=params, payload=None, ttl_hours=ttl_hours, headers=headers)

    def get_text(self, url: str, params: dict | None = None, *, ttl_hours: float | None = None,
                 headers: dict | None = None) -> str:
        """GET a non-JSON body (e.g. full-text XML); cached like JSON responses."""
        headers = {"Accept": "application/xml, text/plain, */*", **(headers or {})}
        return self._request("GET", url, params=params, payload=None, ttl_hours=ttl_hours, headers=headers,
                             mode="text")

    def get_headers(self, url: str, params: dict | None = None, *, ttl_hours: float | None = None) -> dict:
        """Response headers of a GET (lower-case keys), e.g. a release header; cached like bodies."""
        return self._request("GET", url, params=params, payload=None, ttl_hours=ttl_hours, headers=None,
                             mode="headers")

    def post_json(self, url: str, payload: Any, *, ttl_hours: float | None = None,
                  headers: dict | None = None) -> Any:
        return self._request("POST", url, params=None, payload=payload, ttl_hours=ttl_hours, headers=headers)

    # -- internals --------------------------------------------------------
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            session.headers.update({"User-Agent": self.settings.user_agent, "Accept": "application/json"})
            self._local.session = session
        return session

    @staticmethod
    def cache_key(method: str, url: str, params: dict | None, payload: Any) -> str:
        blob = json.dumps([method, url, sorted((params or {}).items()), payload], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _request(self, method: str, url: str, *, params: dict | None, payload: Any,
                 ttl_hours: float | None, headers: dict | None, mode: str = "json") -> Any:
        ttl = self.settings.cache_ttl_hours if ttl_hours is None else ttl_hours
        key = self.cache_key(method + ("" if mode == "json" else f":{mode}"), url, params, payload)
        if self.cache is not None and ttl > 0:
            cached = self.cache.get(key, ttl * 3600)
            if cached is not None:
                return cached

        host = urlparse(url).netloc
        interval = HOST_MIN_INTERVAL.get(host, 0.05)
        if host == "eutils.ncbi.nlm.nih.gov" and self.settings.ncbi_api_key:
            interval = 0.11

        attempts = max(1, self.settings.http_retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._throttle.wait(host, interval)
            try:
                response = self._session().request(
                    method, url, params=params, json=payload, headers=headers,
                    timeout=self.settings.http_timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                log.debug("network error %s %s: %s", method, url, exc)
                self._backoff(attempt, None)
                continue

            if response.status_code in RETRY_STATUSES and attempt < attempts - 1:
                last_error = HttpError(f"HTTP {response.status_code}", response.status_code, url)
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code >= 400:
                snippet = (response.text or "")[:300]
                raise HttpError(f"HTTP {response.status_code} for {url}: {snippet}", response.status_code, url)
            if mode == "text":
                data = response.text
            elif mode == "headers":
                data = {k.lower(): v for k, v in response.headers.items()}
            else:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise HttpError(f"Invalid JSON from {url}", response.status_code, url) from exc
            if self.cache is not None and ttl > 0:
                self.cache.set(key, data)
            return data

        raise HttpError(f"Request failed after {attempts} attempts: {method} {url}: {last_error}", url=url)

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2 ** attempt, 30)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 60))
            except ValueError:
                pass
        self._sleep(delay)
