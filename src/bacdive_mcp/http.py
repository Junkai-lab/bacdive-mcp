"""Polite HTTP: disk cache, concurrency cap, spacing, and short backoff."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from bacdive_mcp import __version__
from bacdive_mcp.errors import BacdiveError

USER_AGENT = f"bacdive-mcp/{__version__} (+https://github.com/Junkai-lab/bacdive-mcp)"
RETRIABLE_STATUS = {429, 500, 502, 503, 504}
SECRET_QUERY_KEYS = {"api_key", "password", "token", "access_token"}


def default_cache_dir() -> Path:
    configured = os.environ.get("BACDIVE_CACHE_DIR")
    if configured:
        return Path(configured)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "bacdive-mcp"


def cache_ttl_seconds() -> float:
    raw = os.environ.get("BACDIVE_CACHE_TTL_DAYS", "7")
    try:
        days = float(raw)
    except ValueError:
        days = 7.0
    return max(0.0, days) * 24 * 60 * 60


def cache_enabled() -> bool:
    return os.environ.get("BACDIVE_CACHE", "1").strip().lower() not in {"0", "false", "no", "off"}


def redact_text(value: str, secrets: list[str] | None = None) -> str:
    redacted = value
    for secret in secrets or []:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


class PoliteHttp:
    """At most two requests in flight, about 0.5 s between starts, three retries."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        ttl_seconds: float = 7 * 24 * 60 * 60,
        min_interval: float = 0.5,
        max_concurrent: int = 2,
        max_retries: int = 3,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        enabled_cache: bool = True,
        secrets: list[str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.cache_dir = cache_dir or default_cache_dir()
        self.ttl_seconds = ttl_seconds
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._sleep = sleeper
        self._clock = clock
        self._cache_on = enabled_cache
        self._secrets = [s for s in (secrets or []) if s]
        self._sem = threading.Semaphore(max_concurrent)
        self._pace_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._next_at = 0.0
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    @classmethod
    def from_env(cls, **kwargs: object) -> PoliteHttp:
        secrets = []
        for name in ("NCBI_API_KEY", "BACDIVE_PASSWORD", "BACDIVE_USER"):
            value = os.environ.get(name)
            if value:
                secrets.append(value)
        return cls(
            cache_dir=default_cache_dir(),
            ttl_seconds=cache_ttl_seconds(),
            enabled_cache=cache_enabled(),
            secrets=secrets,
            **kwargs,  # type: ignore[arg-type]
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str) -> dict:
        status, text = self.request("GET", url, accept="application/json")
        return self._parse_json(status, text)

    def post_json(self, url: str, body: str, *, content_type: str, accept: str) -> dict:
        status, text = self.request(
            "POST",
            url,
            content=body.encode("utf-8"),
            content_type=content_type,
            accept=accept,
        )
        return self._parse_json(status, text)

    def get_text(self, url: str, *, accept: str) -> str:
        _status, text = self.request("GET", url, accept=accept)
        return text

    def request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
    ) -> tuple[int, str]:
        key = self._cache_key(method, url, content)
        cached = self._read_cache(key)
        if cached is not None:
            return cached

        headers = {"Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type

        self._sem.acquire()
        try:
            self._pace()
            last_status: int | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    response = self._client.request(method, url, content=content, headers=headers)
                except httpx.HTTPError:
                    last_status = None
                    if attempt >= self.max_retries:
                        raise BacdiveError(
                            f"{method} {self._safe_target(url)} failed: network error after {self.max_retries} retries.",
                            code="network_error",
                        )
                    self._backoff(attempt, None)
                    continue

                last_status = response.status_code
                if response.status_code in RETRIABLE_STATUS and attempt < self.max_retries:
                    self._backoff(attempt, response)
                    continue
                if response.status_code >= 400:
                    retried = response.status_code in RETRIABLE_STATUS
                    raise BacdiveError(
                        self._http_message(method, url, response.status_code, retried=retried)
                        + self._error_detail(response),
                        code="http_error",
                        status=response.status_code,
                    )
                text = response.text
                self._write_cache(key, response.status_code, text)
                return response.status_code, text

            raise BacdiveError(
                self._http_message(method, url, last_status or 0, retried=True),
                code="http_error",
                status=last_status,
            )
        finally:
            self._sem.release()

    def _pace(self) -> None:
        with self._pace_lock:
            now = self._clock()
            delay = self._next_at - now
            if delay > 0:
                self._sleep(delay)
                now = self._clock()
            self._next_at = now + self.min_interval

    def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        delay = 0.5 * (2**attempt)
        if response is not None and response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    requested = float(retry_after)
                except ValueError:
                    requested = delay
                else:
                    if requested > 8:
                        raise BacdiveError(
                            "BacDive or NCBI asked us to wait longer than 8 seconds (HTTP 429). "
                            "Try again later. No further retries were made.",
                            code="rate_limited",
                            status=429,
                        )
                    delay = max(delay, requested)
        self._sleep(delay)

    def _http_message(self, method: str, url: str, status: int, *, retried: bool) -> str:
        target = self._safe_target(url)
        if retried:
            return (
                f"{method} {target} failed with HTTP {status} after {self.max_retries} retries."
            )
        return f"{method} {target} failed with HTTP {status}."

    def _error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        detail = payload.get("exception") or payload.get("message")
        if not detail:
            return ""
        return " " + redact_text(str(detail), self._secrets)[:300]

    def _safe_target(self, url: str) -> str:
        # Drop the query string so an API key cannot leak, then scrub known secrets.
        path = url.split("?", 1)[0]
        return redact_text(path, self._secrets)

    def _parse_json(self, status: int, text: str) -> dict:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BacdiveError(
                f"Expected JSON (HTTP {status}) but the body was not JSON.",
                code="bad_response",
                status=status,
            ) from exc
        if not isinstance(data, dict):
            raise BacdiveError("Expected a JSON object.", code="bad_response", status=status)
        return data

    def _cache_key(self, method: str, url: str, content: bytes | None) -> str:
        raw = method.upper() + "\n" + url.split("?", 1)[0] + "\n"
        # Query parameters except secrets still change the response (search_type, predictions).
        query = url.split("?", 1)[1] if "?" in url else ""
        if query:
            kept = []
            for part in query.split("&"):
                name = part.split("=", 1)[0]
                if name.lower() in SECRET_QUERY_KEYS:
                    continue
                kept.append(part)
            raw += "&".join(kept)
        raw += "\n"
        if content:
            raw += content.decode("utf-8", "replace")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / key

    def _read_cache(self, key: str) -> tuple[int, str] | None:
        if not self._cache_on:
            return None
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored = float(payload["stored_at"])
            if self._clock() - stored > self.ttl_seconds:
                return None
            return int(payload["status"]), str(payload["body"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, status: int, body: str) -> None:
        if not self._cache_on or status != 200:
            return
        path = self._cache_path(key)
        record = {"stored_at": self._clock(), "status": status, "body": body}
        text = json.dumps(record, ensure_ascii=False)
        with self._cache_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
