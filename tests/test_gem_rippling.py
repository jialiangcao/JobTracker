"""Gem and Rippling: fetcher request shape and adapter mapping.

Both boards are addressed by slug on a single shared host, so these tests pin the two
things most likely to break silently — Gem answering HTTP 200 with a GraphQL `errors`
array, and the apply URL that Gem's payload does not contain.
"""

import json
from typing import Any

import httpx
import pytest
import respx

from jobtrack.adapters.gem import GemAdapter
from jobtrack.adapters.registry import get_adapter
from jobtrack.adapters.rippling import RipplingAdapter
from jobtrack.config import Settings
from jobtrack.sources.base import SourceRef
from jobtrack.sources.gem import GRAPHQL_URL, GemFetcher
from jobtrack.sources.polite_http import FetchError, PoliteClient
from jobtrack.sources.rippling import RipplingFetcher
from tests.payloads import GEM_JOB, RIPPLING_JOB
from tests.test_adapters import raw

RIPPLING_URL = "https://api.rippling.com/platform/api/ats/v1/board/acme/jobs"


def ref(kind: str, slug: str = "acme") -> SourceRef:
    return SourceRef(id=1, kind=kind, name="Acme", config={"slug": slug})


# --- adapters ---------------------------------------------------------------------


def test_gem_adapter() -> None:
    job = GemAdapter().map(raw(GEM_JOB, "gem"))
    assert job is not None
    assert job.external_id == "5175038004"
    assert job.title == "Software Engineering Intern"
    # Not in the payload — built from the injected board slug plus extId.
    assert job.url == "https://jobs.gem.com/acme/5175038004"
    assert job.department == "Content Engineering"
    assert job.employment_type == "INTERN"
    # Both locations are kept so a US-only filter sees the US half.
    assert job.location is not None
    assert "New York, USA" in job.location
    assert "Remote (US)" in job.location
    # locationType is IN_OFFICE, but one location is flagged remote.
    assert job.remote is True
    assert job.description is not None
    assert "Summer 2027" in job.description
    assert "<" not in job.description  # HTML stripped
    assert job.posted_at is not None
    assert job.posted_at.tzinfo is not None


def test_gem_adapter_accepts_string_epoch() -> None:
    """firstPublishedTsSec arrives as an int on some boards and a string on others."""
    as_int = GemAdapter().map(raw(GEM_JOB, "gem"))
    as_str = GemAdapter().map(raw(GEM_JOB | {"firstPublishedTsSec": "1775439282"}, "gem"))
    assert as_int is not None
    assert as_str is not None
    assert as_int.posted_at == as_str.posted_at


def test_gem_adapter_needs_a_board_slug() -> None:
    """Without the fetcher-injected slug there is no addressable URL, so the posting is
    unmappable rather than mapped to a broken link."""
    payload = {k: v for k, v in GEM_JOB.items() if k != "_board_slug"}
    assert GemAdapter().map(raw(payload, "gem")) is None


def test_rippling_adapter() -> None:
    job = RipplingAdapter().map(raw(RIPPLING_JOB, "rippling"))
    assert job is not None
    assert job.external_id == "9a4d79c0-d602-4cb4-a1d3-629b13faaa74"
    assert job.title == "Software Engineering Intern"
    assert job.url == RIPPLING_JOB["url"]
    assert job.location == "Centennial, CO"
    assert job.department == "Build Engineering"
    assert job.remote is False
    # The list endpoint carries neither; posted_at=None means age filtering fails open.
    assert job.description is None
    assert job.posted_at is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Remote - US", True), ("Remote (Canada)", True), ("Centennial, CO", False)],
)
def test_rippling_remote_from_location_label(label: str, expected: bool) -> None:
    payload = RIPPLING_JOB | {"workLocation": {"label": label, "id": label}}
    job = RipplingAdapter().map(raw(payload, "rippling"))
    assert job is not None
    assert job.remote is expected


def test_registry_resolves_both_kinds() -> None:
    assert isinstance(get_adapter("gem"), GemAdapter)
    assert isinstance(get_adapter("rippling"), RipplingAdapter)


# --- fetchers ---------------------------------------------------------------------


@respx.mock
async def test_gem_fetcher_sends_slug_as_board_id(settings: Settings) -> None:
    route = respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"oatsExternalJobPostings": {"jobPostings": [dict(GEM_JOB)]}}}
        )
    )
    async with PoliteClient(settings) as client:
        result = await GemFetcher().fetch(ref("gem"), client)

    body: dict[str, Any] = json.loads(route.calls[0].request.content)
    assert body["variables"] == {"boardId": "acme"}
    assert len(result.postings) == 1
    # The fetcher injects the slug the adapter needs to build a URL.
    assert result.postings[0].payload["_board_slug"] == "acme"


@respx.mock
async def test_gem_fetcher_raises_on_graphql_errors(settings: Settings) -> None:
    """GraphQL reports a renamed field as 200 + errors. Treating that as an empty board
    would disable the source silently instead of tripping the circuit breaker."""
    respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Cannot query extId"}]})
    )
    async with PoliteClient(settings) as client:
        with pytest.raises(FetchError, match="Cannot query extId"):
            await GemFetcher().fetch(ref("gem"), client)


@respx.mock
async def test_rippling_fetcher_reads_bare_array(settings: Settings) -> None:
    respx.get(RIPPLING_URL).mock(return_value=httpx.Response(200, json=[dict(RIPPLING_JOB)]))
    async with PoliteClient(settings) as client:
        result = await RipplingFetcher().fetch(ref("rippling"), client)

    assert len(result.postings) == 1
    assert result.postings[0].payload["uuid"] == RIPPLING_JOB["uuid"]


@respx.mock
async def test_rippling_fetcher_honors_not_modified(settings: Settings) -> None:
    respx.get(RIPPLING_URL).mock(return_value=httpx.Response(304))
    async with PoliteClient(settings) as client:
        result = await RipplingFetcher().fetch(ref("rippling"), client)

    assert result.not_modified is True
    assert result.postings == []
