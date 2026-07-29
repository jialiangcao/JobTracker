import asyncio
import time

import httpx
import pytest
import respx

from jobtrack.config import Settings
from jobtrack.sources.polite_http import FetchError, PoliteClient, Politeness


@respx.mock
async def test_retries_on_429_then_succeeds(settings: Settings) -> None:
    route = respx.get("https://api.example.com/jobs").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    async with PoliteClient(settings) as client:
        response = await client.get("https://api.example.com/jobs")
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_raises_fetch_error_after_exhausting_retries(settings: Settings) -> None:
    route = respx.get("https://api.example.com/jobs").mock(return_value=httpx.Response(503))
    async with PoliteClient(settings) as client:
        with pytest.raises(FetchError) as excinfo:
            await client.get("https://api.example.com/jobs")
    assert route.call_count == settings.max_retries
    assert excinfo.value.http_status == 503


@respx.mock
async def test_non_retryable_responses_are_returned(settings: Settings) -> None:
    respx.get("https://api.example.com/jobs").mock(return_value=httpx.Response(304))
    async with PoliteClient(settings) as client:
        response = await client.get("https://api.example.com/jobs")
    assert response.status_code == 304


@respx.mock
async def test_retries_on_transport_error(settings: Settings) -> None:
    route = respx.get("https://api.example.com/jobs").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})]
    )
    async with PoliteClient(settings) as client:
        response = await client.get("https://api.example.com/jobs")
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_pacing_does_not_hold_cross_host_concurrency(settings: Settings) -> None:
    """A task sleeping until its paced start must not occupy a global slot — one busy host
    would otherwise stall every other host as soon as the per-host caps sum above it."""
    tuned = settings.model_copy(
        update={"min_delay_ms": 200, "max_delay_ms": 200, "global_concurrency": 2}
    )
    respx.get("https://busy.example/jobs").mock(return_value=httpx.Response(200))
    respx.get("https://idle.example/jobs").mock(return_value=httpx.Response(200))

    async with PoliteClient(tuned) as client:
        busy = [asyncio.create_task(client.get("https://busy.example/jobs")) for _ in range(6)]
        await asyncio.sleep(0)  # let the busy host queue up first
        start = time.monotonic()
        await client.get("https://idle.example/jobs")
        elapsed = time.monotonic() - start
        await asyncio.gather(*busy)

    assert elapsed < 0.15


@respx.mock
async def test_source_override_does_not_resize_the_host_budget(settings: Settings) -> None:
    """A per-source max_concurrency only tightens that source. It used to size the host
    semaphore itself, so whichever source touched the host first capped all the rest."""
    tuned = settings.model_copy(update={"per_host_concurrency": 3})
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200)

    respx.get("https://api.example.com/jobs").mock(side_effect=handler)

    async with PoliteClient(tuned) as client:
        # A source pinned to one connection claims the host first.
        await client.get("https://api.example.com/jobs", politeness=Politeness(max_concurrency=1))
        await asyncio.gather(*(client.get("https://api.example.com/jobs") for _ in range(6)))

    assert peak == 3


@respx.mock
async def test_source_override_caps_that_source(settings: Settings) -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200)

    respx.get("https://api.example.com/jobs").mock(side_effect=handler)
    polite = Politeness(max_concurrency=1)

    async with PoliteClient(settings) as client:
        await asyncio.gather(
            *(client.get("https://api.example.com/jobs", politeness=polite) for _ in range(4))
        )

    assert peak == 1


@respx.mock
async def test_robots_allowed(settings: Settings) -> None:
    respx.get("https://site.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    )
    async with PoliteClient(settings) as client:
        assert await client.robots_allowed("https://site.example/careers") is True
        assert await client.robots_allowed("https://site.example/private/x") is False
