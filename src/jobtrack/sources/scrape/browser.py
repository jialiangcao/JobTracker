"""Rendered careers-page fetching and listing-page extraction.

The production registry deliberately constructs :class:`BrowserFetcher` without a renderer,
so scrape sources remain disabled until the browser service is wired in. Tests and future
composition code inject a renderer explicitly.
"""

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Playwright, async_playwright
from selectolax.parser import HTMLParser, Node

from jobtrack.filtering.dedup import canonical_url
from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import to_postings
from jobtrack.sources.polite_http import FetchError, PoliteClient

_DEFAULT_TIMEOUT_MS = 30_000
_SELECTOR_KEYS = frozenset({"item", "title", "url", "location", "description", "posted_at"})


@dataclass(frozen=True)
class RenderedPage:
    """One browser-rendered document."""

    final_url: str
    status_code: int | None
    html: str


class PageRenderer(Protocol):
    """Browser boundary kept injectable so extraction tests need no browser binary."""

    async def render(
        self,
        url: str,
        *,
        wait_for_selector: str | None,
        timeout_ms: int,
        proxy_url: str | None,
    ) -> RenderedPage: ...


PlaywrightFactory = Callable[[], AbstractAsyncContextManager[Playwright]]


class RemotePlaywrightRenderer:
    """Render pages through a remote Playwright browser server.

    The Python client and remote server must use matching Playwright major/minor versions.
    A fresh browser context and page are used for every call so cookies and page state cannot
    leak between sources.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        playwright_factory: PlaywrightFactory = async_playwright,
    ) -> None:
        self._endpoint = endpoint
        self._playwright_factory = playwright_factory

    async def render(
        self,
        url: str,
        *,
        wait_for_selector: str | None,
        timeout_ms: int,
        proxy_url: str | None,
    ) -> RenderedPage:
        async with self._playwright_factory() as playwright:
            browser = await playwright.chromium.connect(self._endpoint, timeout=timeout_ms)
            try:
                if proxy_url is not None:
                    context = await browser.new_context(proxy={"server": proxy_url})
                else:
                    context = await browser.new_context()
                try:
                    page = await context.new_page()
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=timeout_ms
                    )
                    if wait_for_selector is not None:
                        await page.locator(wait_for_selector).first.wait_for(
                            state="attached", timeout=timeout_ms
                        )
                    return RenderedPage(
                        final_url=page.url,
                        status_code=response.status if response is not None else None,
                        html=await page.content(),
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()


class BrowserFetcher:
    def __init__(
        self,
        renderer: PageRenderer | None = None,
        *,
        proxy_pools: Mapping[str, str] | None = None,
    ) -> None:
        self._renderer = renderer
        self._proxy_pools = dict(proxy_pools or {})

    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        if self._renderer is None:
            raise FetchError(
                f"source {source.kind}/{source.name}: browser renderer is not configured"
            )

        url = _source_url(source)
        scrape_config = _scrape_config(source)
        wait_for_selector = _optional_string(scrape_config, "wait_for_selector", source=source)
        timeout_ms = _timeout_ms(scrape_config, source)
        proxy_url = self._resolve_proxy(source)

        if not await client.robots_allowed(url):
            raise FetchError(f"source {source.kind}/{source.name}: robots.txt disallows {url}")

        try:
            rendered = await self._renderer.render(
                url,
                wait_for_selector=wait_for_selector,
                timeout_ms=timeout_ms,
                proxy_url=proxy_url,
            )
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(
                f"source {source.kind}/{source.name}: browser render failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if rendered.status_code is not None and rendered.status_code >= 400:
            raise FetchError(
                f"source {source.kind}/{source.name}: browser navigation returned "
                f"HTTP {rendered.status_code}",
                rendered.status_code,
            )

        payloads, valid_json_ld = _json_ld_payloads(rendered.html, rendered.final_url, source.name)
        if not payloads:
            selectors = scrape_config.get("selectors")
            if selectors is not None:
                payloads = _selector_payloads(
                    rendered.html, rendered.final_url, source.name, selectors, source
                )
            elif not valid_json_ld:
                raise FetchError(
                    f"source {source.kind}/{source.name}: page has no valid JSON-LD "
                    "and config.scrape.selectors is missing"
                )

        payloads = _deduplicate_payloads(payloads)
        return FetchResult(
            postings=to_postings(source, payloads),
            http_status=rendered.status_code,
        )

    def _resolve_proxy(self, source: SourceRef) -> str | None:
        pool = source.config.get("proxy_pool")
        if pool is None:
            return None
        if not isinstance(pool, str) or not pool.strip():
            raise FetchError(
                f"source {source.kind}/{source.name}: config.proxy_pool must be a non-empty string"
            )
        proxy_url = self._proxy_pools.get(pool)
        if proxy_url is None:
            raise FetchError(
                f"source {source.kind}/{source.name}: proxy pool {pool!r} is not configured"
            )
        return proxy_url


def _source_url(source: SourceRef) -> str:
    url = source.config.get("url")
    if not isinstance(url, str) or not url.strip():
        raise FetchError(
            f"source {source.kind}/{source.name}: config.url must be a non-empty HTTP(S) URL"
        )
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise FetchError(
            f"source {source.kind}/{source.name}: config.url must be a valid HTTP(S) URL"
        )
    return url


def _scrape_config(source: SourceRef) -> dict[str, Any]:
    value = source.config.get("scrape", {})
    if not isinstance(value, dict):
        raise FetchError(f"source {source.kind}/{source.name}: config.scrape must be a table")
    return value


def _optional_string(config: dict[str, Any], key: str, *, source: SourceRef) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FetchError(
            f"source {source.kind}/{source.name}: config.scrape.{key} must be a non-empty string"
        )
    return value.strip()


def _timeout_ms(config: dict[str, Any], source: SourceRef) -> int:
    value = config.get("timeout_ms", _DEFAULT_TIMEOUT_MS)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FetchError(
            f"source {source.kind}/{source.name}: config.scrape.timeout_ms "
            "must be a positive integer"
        )
    return value


def _json_ld_payloads(
    html: str, base_url: str, source_name: str
) -> tuple[list[dict[str, Any]], bool]:
    tree = HTMLParser(html)
    payloads: list[dict[str, Any]] = []
    valid_json_ld = False
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            document: Any = json.loads(script.text())
        except (json.JSONDecodeError, TypeError):
            continue
        valid_json_ld = True
        for item in _walk_json_ld(document):
            payload = _json_ld_job(item, base_url, source_name)
            if payload is not None:
                payloads.append(payload)
    return payloads, valid_json_ld


def _walk_json_ld(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_walk_json_ld(item))
    elif isinstance(value, dict):
        if _has_job_posting_type(value.get("@type")):
            found.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list | dict):
            found.extend(_walk_json_ld(graph))
    return found


def _has_job_posting_type(value: Any) -> bool:
    if isinstance(value, str):
        return value.casefold() == "jobposting"
    if isinstance(value, list):
        return any(_has_job_posting_type(item) for item in value)
    return False


def _json_ld_job(item: dict[str, Any], base_url: str, source_name: str) -> dict[str, Any] | None:
    title = _as_text(item.get("title"))
    url = _json_ld_url(item)
    if title is None or url is None:
        return None

    payload: dict[str, Any] = {
        "title": title,
        "url": urljoin(base_url, url),
        "company": _organization_name(item.get("hiringOrganization")) or source_name,
    }
    identifier = _identifier(item.get("identifier"))
    if identifier is not None:
        payload["id"] = identifier
    location = _job_location(item.get("jobLocation"))
    if location is not None:
        payload["location"] = location
    description = _as_text(item.get("description"))
    if description is not None:
        payload["description"] = description
    posted_at = _as_text(item.get("datePosted"))
    if posted_at is not None:
        payload["postedAt"] = posted_at
    return payload


def _json_ld_url(item: dict[str, Any]) -> str | None:
    if url := _as_text(item.get("url")):
        return url
    entity = item.get("mainEntityOfPage")
    if isinstance(entity, dict):
        return _as_text(entity.get("@id")) or _as_text(entity.get("url"))
    return _as_text(entity)


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value") or value.get("@id") or value.get("name")
    if isinstance(value, str | int | float) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _organization_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _as_text(value.get("name"))
    return _as_text(value)


def _job_location(value: Any) -> str | None:
    if isinstance(value, list):
        locations = [_job_location(item) for item in value]
        return " / ".join(location for location in locations if location) or None
    if not isinstance(value, dict):
        return _as_text(value)
    if name := _as_text(value.get("name")):
        return name
    address = value.get("address")
    if not isinstance(address, dict):
        return _as_text(address)
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    parts = (
        _as_text(address.get("addressLocality")),
        _as_text(address.get("addressRegion")),
        _as_text(country),
    )
    return ", ".join(part for part in parts if part) or None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _selector_payloads(
    html: str,
    base_url: str,
    source_name: str,
    raw_selectors: Any,
    source: SourceRef,
) -> list[dict[str, Any]]:
    selectors = _validated_selectors(raw_selectors, source)
    tree = HTMLParser(html)
    try:
        items = tree.css(selectors["item"])
    except ValueError as exc:
        raise FetchError(
            f"source {source.kind}/{source.name}: invalid item CSS selector: {exc}"
        ) from exc
    if not items:
        raise FetchError(
            f"source {source.kind}/{source.name}: item selector "
            f"{selectors['item']!r} matched no elements"
        )

    payloads: list[dict[str, Any]] = []
    for item in items:
        title_node = _select_first(item, selectors["title"], source)
        url_node = _select_first(item, selectors["url"], source)
        title = _node_text(title_node)
        href = url_node.attributes.get("href") if url_node is not None else None
        if title is None or not isinstance(href, str) or not href.strip():
            continue

        payload: dict[str, Any] = {
            "title": title,
            "url": urljoin(base_url, href.strip()),
            "company": source_name,
        }
        for field in ("location", "description"):
            selector = selectors.get(field)
            if selector is not None:
                value = _node_text(_select_first(item, selector, source))
                if value is not None:
                    payload[field] = value
        if selector := selectors.get("posted_at"):
            node = _select_first(item, selector, source)
            if node is not None:
                value = node.attributes.get("datetime") or _node_text(node)
                if value:
                    payload["postedAt"] = value
        payloads.append(payload)
    return payloads


def _validated_selectors(raw: Any, source: SourceRef) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise FetchError(
            f"source {source.kind}/{source.name}: config.scrape.selectors must be a table"
        )
    unknown = set(raw) - _SELECTOR_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise FetchError(
            f"source {source.kind}/{source.name}: unknown scrape selector keys: {names}"
        )
    selectors: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise FetchError(
                f"source {source.kind}/{source.name}: selector {key!r} must be a non-empty string"
            )
        selectors[key] = value.strip()
    missing = {"item", "title", "url"} - selectors.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise FetchError(
            f"source {source.kind}/{source.name}: missing required scrape selectors: {names}"
        )
    return selectors


def _select_first(item: Node, selector: str, source: SourceRef) -> Node | None:
    try:
        return item.css_first(selector)
    except ValueError as exc:
        raise FetchError(
            f"source {source.kind}/{source.name}: invalid CSS selector {selector!r}: {exc}"
        ) from exc


def _node_text(node: Node | None) -> str | None:
    if node is None:
        return None
    text = " ".join(node.text(separator=" ").split())
    return text or None


def _deduplicate_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        url = payload.get("url")
        if not isinstance(url, str):
            continue
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)
    return unique
