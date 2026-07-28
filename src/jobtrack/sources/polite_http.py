"""Shared polite HTTP client: per-host concurrency caps, jittered pacing, retry with
backoff honoring Retry-After, and a robots.txt helper for future scraping sources."""

import asyncio
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from jobtrack.config import Settings
from jobtrack.observability.logging import get_logger

log = get_logger(__name__)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class Politeness:
    """Per-source overrides; None falls back to the global settings defaults."""

    max_concurrency: int | None = None
    min_delay_ms: int | None = None
    max_delay_ms: int | None = None


class _HostState:
    def __init__(self, max_concurrency: int) -> None:
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.lock = asyncio.Lock()
        self.next_at = 0.0  # monotonic time before which no new request may start


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, dt.timestamp() - time.time())


class PoliteClient:
    def __init__(self, settings: Settings, proxy: str | None = None) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            proxy=proxy,
        )
        self._global = asyncio.Semaphore(settings.global_concurrency)
        self._hosts: dict[str, _HostState] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _host_state(self, host: str, politeness: Politeness | None) -> _HostState:
        state = self._hosts.get(host)
        if state is None:
            max_concurrency = (
                politeness.max_concurrency if politeness and politeness.max_concurrency else None
            ) or self._settings.per_host_concurrency
            state = _HostState(max_concurrency)
            self._hosts[host] = state
        return state

    async def _pace(self, state: _HostState, politeness: Politeness | None) -> None:
        """Reserve a start slot: request starts on the same host are spaced by a jittered
        delay, independent of how long each request runs."""
        s = self._settings
        min_ms = (
            politeness.min_delay_ms
            if politeness and politeness.min_delay_ms is not None
            else s.min_delay_ms
        )
        max_ms = (
            politeness.max_delay_ms
            if politeness and politeness.max_delay_ms is not None
            else s.max_delay_ms
        )
        delay = random.uniform(min_ms, max(min_ms, max_ms)) / 1000.0
        async with state.lock:
            now = time.monotonic()
            wait = max(0.0, state.next_at - now)
            state.next_at = max(now, state.next_at) + delay
        if wait > 0:
            await asyncio.sleep(wait)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        politeness: Politeness | None = None,
    ) -> httpx.Response:
        """GET with pacing and retries. Returns any non-retryable response (including
        304/404); raises FetchError once retryable failures exhaust attempts."""
        host = urlsplit(url).netloc.lower()
        state = self._host_state(host, politeness)
        s = self._settings

        last_error = "unknown"
        last_status: int | None = None
        for attempt in range(s.max_retries):
            response: httpx.Response | None = None
            async with self._global, state.semaphore:
                await self._pace(state, politeness)
                try:
                    response = await self._client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"

            if response is not None:
                if response.status_code not in _RETRYABLE_STATUSES:
                    return response
                last_status = response.status_code
                last_error = f"HTTP {response.status_code}"

            if attempt + 1 >= s.max_retries:
                break
            backoff = s.backoff_base_seconds * (2**attempt) + random.uniform(0, 0.5)
            if response is not None and (retry_after := _retry_after_seconds(response)) is not None:
                backoff = max(backoff, retry_after)
            backoff = min(backoff, s.backoff_cap_seconds)
            log.warning("retrying", url=url, attempt=attempt + 1, error=last_error, wait=backoff)
            await asyncio.sleep(backoff)

        raise FetchError(
            f"GET {url} failed after {s.max_retries} attempts: {last_error}", last_status
        )

    async def robots_allowed(self, url: str) -> bool:
        """robots.txt check for scraping sources (API fetchers don't call this).
        Fails open when robots.txt is unavailable."""
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host not in self._robots:
            robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                response = await self._client.get(robots_url)
                if response.status_code == 200:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(response.text.splitlines())
            except httpx.HTTPError:
                parser = None
            self._robots[host] = parser
        parser = self._robots[host]
        return parser is None or parser.can_fetch(self._settings.user_agent, url)
