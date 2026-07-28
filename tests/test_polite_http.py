import httpx
import pytest
import respx

from jobtrack.config import Settings
from jobtrack.sources.polite_http import FetchError, PoliteClient


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
async def test_robots_allowed(settings: Settings) -> None:
    respx.get("https://site.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    )
    async with PoliteClient(settings) as client:
        assert await client.robots_allowed("https://site.example/careers") is True
        assert await client.robots_allowed("https://site.example/private/x") is False
