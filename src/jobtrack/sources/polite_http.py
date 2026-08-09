"""Shared polite HTTP client: per-host concurrency caps, jittered pacing, retry with
backoff honoring Retry-After, and a robots.txt helper for future scraping sources."""

import asyncio
import random
import time
import urllib.robotparser
from contextlib import AsyncExitStack
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from jobtrack.config import Settings
from jobtrack.observability.logging import get_logger

log = get_logger(__name__)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Providers that hand every customer its own subdomain but enforce one rate limit per
# client IP behind them. Pacing and concurrency key on the group, not the hostname —
# otherwise a thousand tenants read as a thousand idle hosts and all start at once.
# Measured: *.myworkdayjobs.com serves 15 req/s indefinitely and starts 429ing at 25.
_SHARED_LIMIT_DOMAINS = ("myworkdayjobs.com",)


def _limit_group(host: str) -> str:
    for domain in _SHARED_LIMIT_DOMAINS:
        if host == domain or host.endswith(f".{domain}"):
            return domain
    return host


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


# A host or group shares one pacing clock, so its delay sets the provider-wide request
# rate. A source declaring its own politeness replaces this entirely.
#
# Workday: the 15 req/s figure above is the *429* ceiling, and staying under it was not
# enough — sustained volume trips bot mitigation instead, which answers HTTP 200 with an
# HTML challenge body rather than a status code. A rate probe cannot see that, so it read
# as success right up until the JSON parse. ~325ms (~3 req/s) held across a full run.
#
# Workable: one host for every board, and 150-400ms (~3.6 req/s) drew 429s across roughly
# half the fleet. ~1.25s (one board per second) was the first rate that held, and 600-900ms
# (~1.33 req/s) is a bisect of that untested gap, taken to shorten the run — 1075 boards on
# one clock was the longest pole in a run that overran its 30-minute interval.
#
# Watch source_results for http_status 429 on workable sources after changing this. A 429
# here is self-defeating rather than merely impolite: it is exempt from the circuit breaker
# (record_source_failure), so nothing gets disabled and nothing goes quiet — the retries
# just add wall clock to the run this delay was lowered to shorten. If they appear, go back
# to 1000-1500 rather than splitting again.
_GROUP_POLITENESS = {
    "myworkdayjobs.com": Politeness(min_delay_ms=250, max_delay_ms=400),
    "apply.workable.com": Politeness(min_delay_ms=600, max_delay_ms=900),
}


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
        self._source_limits: dict[tuple[str, Politeness], asyncio.Semaphore] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _host_state(self, host: str) -> _HostState:
        """One pacing clock and one concurrency cap per host, sized from settings alone.
        A per-source max_concurrency must not resize the budget every other source on the
        same host shares — whichever source happened to arrive first would otherwise set
        it for all of them. Those overrides get their own semaphore in _source_limit."""
        state = self._hosts.get(host)
        if state is None:
            state = _HostState(self._settings.per_host_concurrency)
            self._hosts[host] = state
        return state

    def _source_limit(self, host: str, politeness: Politeness | None) -> asyncio.Semaphore | None:
        """Extra cap for sources setting politeness.max_concurrency. It can only tighten
        the caller, never loosen it: the host cap still applies on top. Sources on a host
        sharing an identical politeness config share one budget."""
        if politeness is None or not politeness.max_concurrency:
            return None
        key = (host, politeness)
        semaphore = self._source_limits.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(politeness.max_concurrency)
            self._source_limits[key] = semaphore
        return semaphore

    async def _pace(self, state: _HostState, politeness: Politeness | None) -> None:
        """Reserve a start slot: request starts on the same host are spaced by a jittered
        delay, independent of how long each request runs. Callers must await this outside
        the semaphores — a task sleeping until its slot holds no concurrency budget, so
        the delay sets the request rate and the semaphores cap only what is in flight."""
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
        return await self._request("GET", url, headers=headers, politeness=politeness)

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        politeness: Politeness | None = None,
    ) -> httpx.Response:
        """POST with the same pacing and retries as `get`.

        Only for endpoints where POST is a *read* — the retry loop replays the request
        verbatim, so anything with side effects would be repeated. Workday's search
        endpoint, the one caller today, takes its query in the body and mutates nothing.
        """
        return await self._request("POST", url, json=json, headers=headers, politeness=politeness)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        politeness: Politeness | None = None,
    ) -> httpx.Response:
        host = _limit_group(urlsplit(url).netloc.lower())
        if politeness is None:
            politeness = _GROUP_POLITENESS.get(host)
        state = self._host_state(host)
        source_limit = self._source_limit(host, politeness)
        s = self._settings

        last_error = "unknown"
        last_status: int | None = None
        for attempt in range(s.max_retries):
            response: httpx.Response | None = None
            await self._pace(state, politeness)
            # Narrowest budget first, so waiting on it never pins a host or global slot.
            async with AsyncExitStack() as stack:
                if source_limit is not None:
                    await stack.enter_async_context(source_limit)
                await stack.enter_async_context(self._global)
                await stack.enter_async_context(state.semaphore)
                try:
                    response = await self._client.request(method, url, headers=headers, json=json)
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
            f"{method} {url} failed after {s.max_retries} attempts: {last_error}", last_status
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
