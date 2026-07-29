"""Isolated careers-page scraping tests: no network and no browser binary."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Playwright

from jobtrack.sources.base import SourceRef
from jobtrack.sources.polite_http import FetchError, PoliteClient
from jobtrack.sources.scrape.browser import (
    BrowserFetcher,
    PageRenderer,
    PlaywrightFactory,
    RemotePlaywrightRenderer,
    RenderedPage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "careers"


@dataclass
class FakeRenderer:
    page: RenderedPage | None = None
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def render(
        self,
        url: str,
        *,
        wait_for_selector: str | None,
        timeout_ms: int,
        proxy_url: str | None,
    ) -> RenderedPage:
        self.calls.append(
            {
                "url": url,
                "wait_for_selector": wait_for_selector,
                "timeout_ms": timeout_ms,
                "proxy_url": proxy_url,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.page is not None
        return self.page


@dataclass
class FakeRobotsClient:
    allowed: bool = True
    calls: list[str] = field(default_factory=list)

    async def robots_allowed(self, url: str) -> bool:
        self.calls.append(url)
        return self.allowed


def source(config: dict[str, Any] | None = None) -> SourceRef:
    return SourceRef(
        id=7,
        kind="scrape",
        name="Example",
        config=config or {"url": "https://careers.example/jobs"},
    )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def client(fake: FakeRobotsClient) -> PoliteClient:
    return cast(PoliteClient, fake)


def renderer(fake: FakeRenderer) -> PageRenderer:
    return cast(PageRenderer, fake)


async def test_json_ld_extracts_supported_shapes_and_deduplicates() -> None:
    fake_renderer = FakeRenderer(
        RenderedPage(
            final_url="https://careers.example/teams/",
            status_code=200,
            html=fixture("json_ld.html"),
        )
    )

    result = await BrowserFetcher(renderer(fake_renderer)).fetch(
        source(), client(FakeRobotsClient())
    )

    assert result.http_status == 200
    assert len(result.postings) == 2
    first, second = result.postings
    assert first.source_id == 7
    assert first.source_kind == "scrape"
    assert first.source_name == "Example"
    assert first.payload == {
        "title": "Software Engineering Intern",
        "url": "https://careers.example/jobs/one?utm_source=careers",
        "company": "Example Labs",
        "id": "job-1",
        "location": "New York, NY, US",
        "description": "<p>Build useful things.</p>",
        "postedAt": "2026-07-20",
    }
    assert second.payload["title"] == "Data Science Intern"
    assert second.payload["url"] == "https://careers.example/teams/two"
    assert second.payload["id"] == "202"
    assert second.payload["location"] == "Boston, MA / Seattle, WA"


async def test_json_ld_takes_precedence_over_configured_selectors() -> None:
    fake_renderer = FakeRenderer(
        RenderedPage("https://careers.example/", 200, fixture("json_ld.html"))
    )
    configured = source(
        {
            "url": "https://careers.example/",
            "scrape": {
                "selectors": {
                    "item": ".does-not-exist",
                    "title": ".title",
                    "url": "a",
                }
            },
        }
    )

    result = await BrowserFetcher(renderer(fake_renderer)).fetch(
        configured, client(FakeRobotsClient())
    )

    assert len(result.postings) == 2


async def test_valid_json_ld_without_jobs_is_a_successful_empty_page() -> None:
    fake_renderer = FakeRenderer(
        RenderedPage("https://careers.example/", 200, fixture("empty_json_ld.html"))
    )

    result = await BrowserFetcher(renderer(fake_renderer)).fetch(
        source(), client(FakeRobotsClient())
    )

    assert result.postings == []


async def test_selector_fallback_extracts_optional_fields_and_skips_unusable_cards() -> None:
    fake_renderer = FakeRenderer(
        RenderedPage(
            "https://jobs.example/careers/",
            200,
            fixture("selectors.html"),
        )
    )
    configured = source(
        {
            "url": "https://jobs.example/careers",
            "proxy_pool": "residential",
            "scrape": {
                "wait_for_selector": ".jobs",
                "timeout_ms": 12_345,
                "selectors": {
                    "item": ".job-card",
                    "title": ".title",
                    "url": "a.details",
                    "location": ".location",
                    "description": ".summary",
                    "posted_at": "time",
                },
            },
        }
    )

    result = await BrowserFetcher(
        renderer(fake_renderer),
        proxy_pools={"residential": "http://proxy.example:8080"},
    ).fetch(configured, client(FakeRobotsClient()))

    assert len(result.postings) == 2
    assert result.postings[0].payload == {
        "title": "Platform Engineering Intern",
        "url": "https://jobs.example/positions/platform?utm_source=list",
        "company": "Example",
        "location": "Austin, TX",
        "description": "Work on the platform.",
        "postedAt": "2026-07-22",
    }
    assert result.postings[1].payload == {
        "title": "Security Intern",
        "url": "https://jobs.example/careers/security",
        "company": "Example",
    }
    assert fake_renderer.calls == [
        {
            "url": "https://jobs.example/careers",
            "wait_for_selector": ".jobs",
            "timeout_ms": 12_345,
            "proxy_url": "http://proxy.example:8080",
        }
    ]


@pytest.mark.parametrize(
    ("selectors", "message"),
    [
        (None, "selectors is missing"),
        ([], "selectors must be a table"),
        ({"item": ".job"}, "missing required scrape selectors: title, url"),
        (
            {"item": ".job", "title": ".title", "url": "a", "mystery": ".x"},
            "unknown scrape selector keys: mystery",
        ),
        (
            {"item": ".job", "title": "", "url": "a"},
            "selector 'title' must be a non-empty string",
        ),
    ],
)
async def test_invalid_or_missing_selector_configuration_fails(
    selectors: object, message: str
) -> None:
    fake_renderer = FakeRenderer(RenderedPage("https://careers.example/", 200, "<html></html>"))
    scrape: dict[str, object] = {}
    if selectors is not None:
        scrape["selectors"] = selectors

    with pytest.raises(FetchError, match=message):
        await BrowserFetcher(renderer(fake_renderer)).fetch(
            source({"url": "https://careers.example/", "scrape": scrape}),
            client(FakeRobotsClient()),
        )


async def test_configured_item_selector_matching_nothing_fails() -> None:
    fake_renderer = FakeRenderer(
        RenderedPage("https://careers.example/", 200, fixture("selectors.html"))
    )
    configured = source(
        {
            "url": "https://careers.example/",
            "scrape": {
                "selectors": {
                    "item": ".missing",
                    "title": ".title",
                    "url": "a",
                }
            },
        }
    )

    with pytest.raises(FetchError, match="matched no elements"):
        await BrowserFetcher(renderer(fake_renderer)).fetch(configured, client(FakeRobotsClient()))


async def test_invalid_optional_css_selector_is_a_fetch_error() -> None:
    fake_renderer = FakeRenderer(
        RenderedPage("https://careers.example/", 200, fixture("selectors.html"))
    )
    configured = source(
        {
            "url": "https://careers.example/",
            "scrape": {
                "selectors": {
                    "item": ".job-card",
                    "title": ".title",
                    "url": "a",
                    "location": "div>>>span",
                }
            },
        }
    )

    with pytest.raises(FetchError, match="invalid CSS selector"):
        await BrowserFetcher(renderer(fake_renderer)).fetch(configured, client(FakeRobotsClient()))


async def test_unconfigured_fetcher_fails_without_touching_client() -> None:
    robots = FakeRobotsClient()

    with pytest.raises(FetchError, match="browser renderer is not configured"):
        await BrowserFetcher().fetch(source(), client(robots))

    assert robots.calls == []


@pytest.mark.parametrize(
    "url",
    ["", "careers.example/jobs", "ftp://careers.example/jobs", "https:///jobs"],
)
async def test_invalid_source_url_fails(url: str) -> None:
    fake_renderer = FakeRenderer(RenderedPage("https://example.test/", 200, ""))

    with pytest.raises(FetchError, match=r"config.url must be"):
        await BrowserFetcher(renderer(fake_renderer)).fetch(
            source({"url": url}), client(FakeRobotsClient())
        )


async def test_robots_denial_stops_before_rendering() -> None:
    fake_renderer = FakeRenderer(RenderedPage("https://example.test/", 200, ""))
    robots = FakeRobotsClient(allowed=False)

    with pytest.raises(FetchError, match=r"robots\.txt disallows"):
        await BrowserFetcher(renderer(fake_renderer)).fetch(source(), client(robots))

    assert fake_renderer.calls == []


async def test_navigation_http_failure_preserves_status() -> None:
    fake_renderer = FakeRenderer(RenderedPage("https://careers.example/jobs", 503, "<html></html>"))

    with pytest.raises(FetchError, match="HTTP 503") as caught:
        await BrowserFetcher(renderer(fake_renderer)).fetch(source(), client(FakeRobotsClient()))

    assert caught.value.http_status == 503


async def test_renderer_error_is_translated_with_source_context() -> None:
    fake_renderer = FakeRenderer(error=TimeoutError("navigation timed out"))

    with pytest.raises(
        FetchError,
        match=r"source scrape/Example: browser render failed: TimeoutError: navigation timed out",
    ):
        await BrowserFetcher(renderer(fake_renderer)).fetch(source(), client(FakeRobotsClient()))


async def test_unknown_proxy_pool_fails_before_robots_or_rendering() -> None:
    fake_renderer = FakeRenderer(RenderedPage("https://example.test/", 200, ""))
    robots = FakeRobotsClient()

    with pytest.raises(FetchError, match="proxy pool 'missing' is not configured"):
        await BrowserFetcher(renderer(fake_renderer)).fetch(
            source({"url": "https://careers.example/", "proxy_pool": "missing"}),
            client(robots),
        )

    assert robots.calls == []
    assert fake_renderer.calls == []


class FakeResponse:
    status = 201


class FakeFirstLocator:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def wait_for(self, **options: str | int) -> None:
        self._events.append(("wait_for", options["state"], options["timeout"]))


class FakeLocator:
    def __init__(self, events: list[object]) -> None:
        self.first = FakeFirstLocator(events)


class FakePage:
    url = "https://careers.example/final"

    def __init__(self, events: list[object], *, content_error: Exception | None = None) -> None:
        self._events = events
        self._content_error = content_error

    async def goto(self, url: str, **options: str | int) -> FakeResponse:
        self._events.append(("goto", url, options["wait_until"], options["timeout"]))
        return FakeResponse()

    def locator(self, selector: str) -> FakeLocator:
        self._events.append(("locator", selector))
        return FakeLocator(self._events)

    async def content(self) -> str:
        self._events.append("content")
        if self._content_error is not None:
            raise self._content_error
        return "<html>rendered</html>"


class FakeContext:
    def __init__(self, events: list[object], page: FakePage) -> None:
        self._events = events
        self._page = page

    async def new_page(self) -> FakePage:
        self._events.append("new_page")
        return self._page

    async def close(self) -> None:
        self._events.append("context_close")


class FakeBrowser:
    def __init__(self, events: list[object], context: FakeContext) -> None:
        self._events = events
        self._context = context

    async def new_context(self, **options: object) -> FakeContext:
        self._events.append(("new_context", options))
        return self._context

    async def close(self) -> None:
        self._events.append("browser_close")


class FakeChromium:
    def __init__(self, events: list[object], browser: FakeBrowser) -> None:
        self._events = events
        self._browser = browser

    async def connect(self, endpoint: str, **options: int) -> FakeBrowser:
        self._events.append(("connect", endpoint, options["timeout"]))
        return self._browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


def remote_renderer_fakes(
    *, content_error: Exception | None = None
) -> tuple[RemotePlaywrightRenderer, list[object]]:
    events: list[object] = []
    page = FakePage(events, content_error=content_error)
    context = FakeContext(events, page)
    browser = FakeBrowser(events, context)
    fake_playwright = FakePlaywright(FakeChromium(events, browser))

    @asynccontextmanager
    async def factory() -> AsyncGenerator[Playwright]:
        events.append("playwright_enter")
        try:
            yield cast(Playwright, fake_playwright)
        finally:
            events.append("playwright_exit")

    return RemotePlaywrightRenderer(
        "ws://browser:3000/playwright",
        playwright_factory=cast(PlaywrightFactory, factory),
    ), events


async def test_remote_renderer_lifecycle_and_options() -> None:
    remote, events = remote_renderer_fakes()

    result = await remote.render(
        "https://careers.example/jobs",
        wait_for_selector=".jobs",
        timeout_ms=9_000,
        proxy_url="http://proxy.example:8080",
    )

    assert result == RenderedPage(
        final_url="https://careers.example/final",
        status_code=201,
        html="<html>rendered</html>",
    )
    assert events == [
        "playwright_enter",
        ("connect", "ws://browser:3000/playwright", 9_000),
        ("new_context", {"proxy": {"server": "http://proxy.example:8080"}}),
        "new_page",
        ("goto", "https://careers.example/jobs", "domcontentloaded", 9_000),
        ("locator", ".jobs"),
        ("wait_for", "attached", 9_000),
        "content",
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


async def test_remote_renderer_cleans_up_after_page_failure() -> None:
    remote, events = remote_renderer_fakes(content_error=RuntimeError("content failed"))

    with pytest.raises(RuntimeError, match="content failed"):
        await remote.render(
            "https://careers.example/jobs",
            wait_for_selector=None,
            timeout_ms=9_000,
            proxy_url=None,
        )

    assert "context_close" in events
    assert "browser_close" in events
    assert events[-1] == "playwright_exit"
