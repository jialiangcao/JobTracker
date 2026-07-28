"""Shared helpers for JSON API fetchers."""

from datetime import UTC, datetime
from typing import Any

import httpx

from jobtrack.schema import RawPosting
from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.polite_http import FetchError, PoliteClient


def require_slug(source: SourceRef) -> str:
    slug = source.config.get("slug") or source.config.get("board_token")
    if not isinstance(slug, str) or not slug:
        raise FetchError(f"source {source.kind}/{source.name}: config.slug missing")
    return slug


async def get_json(
    source: SourceRef, client: PoliteClient, url: str, *, conditional: bool = True
) -> tuple[Any, httpx.Response] | tuple[None, httpx.Response]:
    """GET a JSON endpoint. Returns (None, response) on 304, (parsed, response) on 200,
    raises FetchError on anything else."""
    response = await client.get(
        url,
        headers=source.conditional_headers() if conditional else None,
        politeness=source.politeness(),
    )
    if response.status_code == 304:
        return None, response
    if response.status_code != 200:
        raise FetchError(f"GET {url} returned HTTP {response.status_code}", response.status_code)
    return response.json(), response


def result_from(response: httpx.Response, postings: list[RawPosting]) -> FetchResult:
    return FetchResult(
        postings=postings,
        http_status=response.status_code,
        not_modified=response.status_code == 304,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )


def to_postings(source: SourceRef, items: list[Any]) -> list[RawPosting]:
    fetched_at = datetime.now(UTC)
    return [
        RawPosting(
            source_id=source.id,
            source_kind=source.kind,
            source_name=source.name,
            payload=item,
            fetched_at=fetched_at,
        )
        for item in items
        if isinstance(item, dict)
    ]
