"""Shared HTTP layer for biomedical retrieval.

* per-host rate limiting (NCBI: 3 req/s without key, 10 with key)
* bounded retries with exponential backoff (tenacity) on 429/5xx/transport errors
* explicit timeouts
* optional on-disk response cache (disabled in deployment mode, where no
  evaluation-derived query may be persisted). An in-memory cache is always
  used within one process run.
* response validation: an HTTP 200 whose body reports a backend failure (e.g. an NCBI
  E-utilities ``"ERROR"`` field) or is malformed is retried a bounded number of times,
  never cached, and finally raised as ``ResponseValidationError``. Cached entries are
  re-validated on read and evicted when invalid, so a failure cached by an older
  version can never be served as a valid (e.g. zero-result) search.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
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


class ResponseValidationError(Exception):
    """A response with an OK HTTP status whose body reports a failure or is malformed/incomplete.

    Such a response is a FAILED retrieval (nothing can be concluded), never an empty result. It is not cached."""

    def __init__(self, reason: str, url: str = "", retryable: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.url = url
        self.retryable = retryable


Validator = Callable[["HTTPResult"], None]


def embedded_api_error(host: str, text: str) -> str:
    """Backend failure reported inside an otherwise successful response body (defence in depth for every caller;
    endpoint-specific validators check more). Returns the error text, or '' if none is detected."""
    if host == "eutils.ncbi.nlm.nih.gov":
        head = text.lstrip()[:4000]
        if head.startswith("{"):
            try:
                d = json.loads(text)
            except ValueError:
                return "malformed JSON"
            if isinstance(d, dict):
                for k in ("ERROR", "error"):
                    if d.get(k):
                        return str(d[k])[:300]
                for sect in ("esearchresult", "result", "header"):
                    v = d.get(sect)
                    if isinstance(v, dict) and v.get("ERROR"):
                        return str(v["ERROR"])[:300]
        elif "<ERROR>" in head:
            return head.split("<ERROR>", 1)[1].split("</ERROR>", 1)[0][:300]
    if host == "gnomad.broadinstitute.org" and text.lstrip().startswith("{"):
        try:
            d = json.loads(text)
        except ValueError:
            return "malformed JSON"
        if isinstance(d, dict) and d.get("errors"):
            return str(d["errors"])[:300]
    return ""


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
    location: str = ""  # redirect target when follow_redirects=False

    def json(self) -> Any:
        return json.loads(self.text)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableHTTPError, httpx.TransportError))


class HttpClient:
    _swept: set[str] = set()  # cache directories already swept for invalid entries in this process

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
        self.cache_evictions = 0  # cached entries rejected on read (embedded error / failed validation)
        self.validation_failures: list[dict[str, str]] = []  # host + reason only (no query values)
        self.validation_attempts = 3
        self.validation_backoff_s = 2.0
        if self.settings.disk_cache_enabled and str(self.settings.cache_dir) not in HttpClient._swept:
            HttpClient._swept.add(str(self.settings.cache_dir))
            n = self.purge_invalid_cache()
            if n:
                log.warning("removed %d cached responses that report a backend error", n)
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

    def purge_invalid_cache(self) -> int:
        """Delete every disk-cache entry whose body reports a backend error (e.g. entries written by versions that cached
        an NCBI 'ERROR' response as a valid search). Returns the number removed."""
        root = self.settings.cache_dir / "http"
        if not root.exists():
            return 0
        n = 0
        for p in root.rglob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                bad = d.get("status") == 200 and bool(embedded_api_error(urlparse(d.get("url", "")).netloc, d.get("text", "")))
            except Exception:
                bad = True  # unreadable entry
            if bad:
                p.unlink(missing_ok=True)
                n += 1
        self.cache_evictions += n
        return n

    def _cache_evict(self, key: str) -> None:
        self._mem.pop(key, None)
        self.cache_evictions += 1
        if self.settings.disk_cache_enabled:
            try:
                self._cache_path(key).unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass

    @staticmethod
    def _valid(r: HTTPResult, host: str, validate: Validator | None) -> bool:
        if r.status == 200 and embedded_api_error(host, r.text):
            return False
        if validate is not None and r.status == 200:
            try:
                validate(r)
            except ResponseValidationError:
                return False
        return True

    def _cache_put(self, key: str, r: HTTPResult) -> None:
        if r.status == 200 and embedded_api_error(urlparse(r.url).netloc, r.text):
            return  # a reported backend failure is never cached (memory or disk)
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
        follow_redirects: bool = True,
        validate: Validator | None = None,
    ) -> HTTPResult:
        """GET/POST with rate limiting, retries and caching.

        `validate` (optional) inspects a status-200 response and raises ResponseValidationError when the body reports a
        failure or is malformed/incomplete. Invalid responses are retried (bounded), never cached, and finally raised;
        invalid cached entries are evicted and re-fetched."""
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
            json.dumps([method, url, sorted(key_params.items()), json_body, follow_redirects], sort_keys=True, default=str).encode()
        ).hexdigest()
        if use_cache:
            cached = self._cache_get(key, host)
            if cached is not None:
                if self._valid(cached, host, validate):
                    return cached
                log.warning("evicting invalid cached response from %s", host)
                self._cache_evict(key)
        last: ResponseValidationError | None = None
        for attempt in range(1, max(1, self.validation_attempts) + 1):
            r = self._do(method, url, params, json_body, headers, host, ok_statuses, follow_redirects)
            try:
                if r.status == 200:
                    err = embedded_api_error(host, r.text)
                    if err:
                        raise ResponseValidationError(f"backend error reported in HTTP 200 body: {err}", r.url)
                    if validate is not None:
                        validate(r)
            except ResponseValidationError as e:
                e.url = e.url or r.url
                last = e
                self.validation_failures.append({"host": host, "reason": e.reason[:200], "attempt": str(attempt)})
                log.warning("invalid response from %s (attempt %d): %s", host, attempt, e.reason[:120])
                if not e.retryable or attempt >= self.validation_attempts:
                    break
                time.sleep(self.validation_backoff_s * attempt)
                continue
            if use_cache:
                self._cache_put(key, r)
            return r
        assert last is not None
        raise last

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1.0, max=20.0),
        reraise=True,
    )
    def _do(self, method, url, params, json_body, headers, host, ok_statuses, follow_redirects=True) -> HTTPResult:
        self._limiter.wait(host, self._rates.get(host, 3.0))
        self.request_count += 1
        resp = self._client.request(method, url, params=params or None, json=json_body, headers=headers, follow_redirects=follow_redirects)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableHTTPError(f"{resp.status_code} from {host}")
        fetched = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        public_url = str(resp.request.url.copy_remove_param("api_key").copy_remove_param("email"))
        out = HTTPResult(public_url, resp.status_code, resp.text, fetched, False)
        out.location = resp.headers.get("location", "")
        return out

    def get_json(self, url: str, **kw) -> Any:
        r = self.request("GET", url, **kw)
        if r.status not in kw.get("ok_statuses", (200,)):
            raise HTTPFetchError(r.status, r.url)
        return r.json()

    def close(self) -> None:
        self._client.close()
