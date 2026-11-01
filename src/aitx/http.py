"""Shared HTTP layer for biomedical retrieval.

* per-host rate limiting (NCBI: 3 req/s without key, 10 with key)
* bounded retries with exponential backoff (tenacity) on 429/5xx/transport errors
* explicit timeouts
* optional on-disk response cache (disabled in deployment mode, where no
  evaluation-derived query may be persisted). An in-memory cache is always
  used within one process run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import Settings, get_settings

log = logging.getLogger(__name__)

USER_AGENT = "aitx-actionability/0.1 (research; SAIL AI-Tx Challenge; +https://aitxchallenge.org)"

# requests per second per host
DEFAULT_RATES = {
    "eutils.ncbi.nlm.nih.gov": 3.0,
    "rest.ensembl.org": 10.0,
    "rest.variantvalidator.org": 2.0,
    "clinicaltrials.gov": 5.0,
    "www.ebi.ac.uk": 5.0,
    "rest.uniprot.org": 5.0,
    "search.clinicalgenome.org": 2.0,
    "gnomad.broadinstitute.org": 1.0,
    "api.fda.gov": 4.0,
    "dailymed.nlm.nih.gov": 4.0,
    "api.clinpgx.org": 2.0,
}
TTL_SECONDS = {
    "clinicaltrials.gov": 12 * 3600,  # trial status changes; keep short
}
DEFAULT_TTL = 7 * 24 * 3600


class RetryableHTTPError(Exception):
    pass


class HTTPFetchError(Exception):
    """Non-retryable HTTP failure (4xx other than 429)."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class _RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str, rate: float) -> None:
        interval = 1.0 / rate if rate > 0 else 0.0
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next.get(host, 0.0))
            self._next[host] = t + interval
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass
class HTTPResult:
    url: str
    status: int
    text: str
    fetched_at: str
    from_cache: bool

    def json(self) -> Any:
        return json.loads(self.text)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableHTTPError, httpx.TransportError))


class HttpClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.settings.http_timeout_s, connect=15.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            transport=transport,
        )
        self._limiter = _RateLimiter()
        self._mem: dict[str, HTTPResult] = {}
        self.request_count = 0
        rates = dict(DEFAULT_RATES)
        if self.settings.ncbi_api_key:
            rates["eutils.ncbi.nlm.nih.gov"] = 9.0
        self._rates = rates

    # ---------------------------------------------------------------- cache
    def _cache_path(self, key: str) -> Path:
        return self.settings.cache_dir / "http" / key[:2] / f"{key}.json"

    def _cache_get(self, key: str, host: str) -> HTTPResult | None:
        if key in self._mem:
            return self._mem[key]
        if not self.settings.disk_cache_enabled:
            return None
        p = self._cache_path(key)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if time.time() - d.get("stored", 0) > TTL_SECONDS.get(host, DEFAULT_TTL):
            return None
        r = HTTPResult(d["url"], d["status"], d["text"], d["fetched_at"], True)
        self._mem[key] = r
        return r

    def _cache_put(self, key: str, r: HTTPResult) -> None:
        self._mem[key] = r
        if not self.settings.disk_cache_enabled or r.status != 200:
            return
        p = self._cache_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"url": r.url, "status": r.status, "text": r.text, "fetched_at": r.fetched_at, "stored": time.time()}),
            encoding="utf-8",
        )

    # -------------------------------------------------------------- request
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> HTTPResult:
        host = urlparse(url).netloc
        params = dict(params or {})
        if host == "eutils.ncbi.nlm.nih.gov":
            params.setdefault("tool", self.settings.ncbi_tool)
            if self.settings.ncbi_email:
                params.setdefault("email", self.settings.ncbi_email)
            if self.settings.ncbi_api_key:
                params.setdefault("api_key", self.settings.ncbi_api_key)
        # cache key excludes credentials
        key_params = {k: v for k, v in params.items() if k not in ("api_key", "email")}
        key = hashlib.sha256(
            json.dumps([method, url, sorted(key_params.items()), json_body], sort_keys=True, default=str).encode()
        ).hexdigest()
        if use_cache:
            cached = self._cache_get(key, host)
            if cached is not None:
                return cached
        r = self._do(method, url, params, json_body, headers, host, ok_statuses)
        if use_cache:
            self._cache_put(key, r)
        return r

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1.0, max=20.0),
        reraise=True,
    )
    def _do(self, method, url, params, json_body, headers, host, ok_statuses) -> HTTPResult:
        self._limiter.wait(host, self._rates.get(host, 3.0))
        self.request_count += 1
        resp = self._client.request(method, url, params=params or None, json=json_body, headers=headers)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableHTTPError(f"{resp.status_code} from {host}")
        fetched = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        public_url = str(resp.request.url.copy_remove_param("api_key").copy_remove_param("email"))
        return HTTPResult(public_url, resp.status_code, resp.text, fetched, False)

    def get_json(self, url: str, **kw) -> Any:
        r = self.request("GET", url, **kw)
        if r.status not in kw.get("ok_statuses", (200,)):
            raise HTTPFetchError(r.status, r.url)
        return r.json()

    def close(self) -> None:
        self._client.close()
