"""Workday fetcher: URL parsing, facet narrowing, and the paging safety valve."""

import json
from typing import Any

import httpx
import pytest
import respx

from jobtrack.config import Settings
from jobtrack.sources.base import SourceRef
from jobtrack.sources.polite_http import FetchError, PoliteClient
from jobtrack.sources.workday import WorkdayFetcher, coordinates
from tests.payloads import WORKDAY_LIST_PAGE

JOBS_URL = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs"
CAREERS_URL = "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers"


def ref(**config: Any) -> SourceRef:
    return SourceRef(id=1, kind="workday", name="Acme", config={"url": CAREERS_URL} | config)


def page(count: int, *, total: int, facets: list[Any] | None = None) -> dict[str, Any]:
    return {
        "total": total,
        "jobPostings": [
            {
                "title": f"Software Engineering Intern {i}",
                "externalPath": f"/job/US-CA-Santa-Clara/Intern-{i}_JR{i:07d}",
                "locationsText": "US, CA, Santa Clara",
                "postedOn": "Posted Today",
                "bulletFields": [f"JR{i:07d}"],
            }
            for i in range(count)
        ],
        "facets": facets or [],
    }


def bodies(route: respx.Route) -> list[Any]:
    return [json.loads(call.request.content) for call in route.calls]


def test_coordinates_from_a_careers_url() -> None:
    coords = coordinates(ref())
    assert coords.jobs_url == JOBS_URL
    assert coords.base_url == CAREERS_URL


def test_coordinates_overrides_and_missing_locale() -> None:
    coords = coordinates(ref(url="https://careers.example.com/External", tenant="acme-inc"))
    assert coords.jobs_url == "https://careers.example.com/wday/cxs/acme-inc/External/jobs"
    # No locale segment in the URL, so the public prefix falls back to en-US.
    assert coords.base_url == "https://careers.example.com/en-US/External"

    explicit = coordinates(ref(site="Other_Site"))
    assert explicit.jobs_url.endswith("/acme/Other_Site/jobs")


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"url": ""}, "config.url missing"),
        ({"url": "https:///en-US/Site"}, "no host"),
        ({"url": "https://acme.wd5.myworkdayjobs.com/en-US"}, "could not derive a site"),
    ],
)
def test_coordinates_rejects_unusable_urls(config: dict[str, Any], message: str) -> None:
    source = SourceRef(id=1, kind="workday", name="Acme", config=config)
    with pytest.raises(FetchError, match=message):
        coordinates(source)


@respx.mock
async def test_probe_narrows_the_crawl_to_intern_facets(settings: Settings) -> None:
    """The load-bearing behavior: the ids are read out of the probe's facet block and
    applied to every subsequent page, so a 2000-posting board costs a handful of requests."""
    route = respx.post(JOBS_URL).mock(
        side_effect=[
            httpx.Response(200, json=WORKDAY_LIST_PAGE),
            httpx.Response(200, json=page(2, total=2)),
        ]
    )
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(), client)

    assert route.call_count == 2
    probe_body, faceted_body = bodies(route)
    assert probe_body["appliedFacets"] == {}
    assert probe_body["limit"] == 20  # server rejects anything larger
    # "New College Graduate" and "Intern (Fixed Term)" matched; "Regular Employee" did not.
    assert faceted_body["appliedFacets"] == {"workerSubType": ["ncg-id", "intern-id"]}
    assert faceted_body["offset"] == 0
    assert len(result.postings) == 2
    assert result.http_status == 200


@respx.mock
async def test_configured_facets_skip_the_probe(settings: Settings) -> None:
    route = respx.post(JOBS_URL).mock(return_value=httpx.Response(200, json=page(1, total=1)))
    async with PoliteClient(settings) as client:
        await WorkdayFetcher().fetch(ref(worker_sub_types=["custom-id"]), client)

    assert route.call_count == 1
    assert bodies(route)[0]["appliedFacets"] == {"workerSubType": ["custom-id"]}


@respx.mock
async def test_falls_back_to_job_family_when_worker_sub_type_has_nothing(
    settings: Settings,
) -> None:
    """Only ~40% of boards classify interns under workerSubType; the rest use
    jobFamilyGroup or jobFamily, which the probe must also mine."""
    facets = [
        {
            "facetParameter": "workerSubType",
            "values": [{"id": "regular-id", "descriptor": "Regular Employee", "count": 400}],
        },
        {
            "facetParameter": "jobFamilyGroup",
            "values": [
                {"id": "eng-id", "descriptor": "Engineering", "count": 300},
                {"id": "intern-group", "descriptor": "Internships & Skillbridge", "count": 12},
            ],
        },
    ]
    route = respx.post(JOBS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(20, total=400, facets=facets)),
            httpx.Response(200, json=page(12, total=12)),
        ]
    )
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(), client)

    assert bodies(route)[1]["appliedFacets"] == {"jobFamilyGroup": ["intern-group"]}
    assert len(result.postings) == 12


@respx.mock
async def test_ambiguous_job_family_descriptors_are_not_treated_as_early_career(
    settings: Settings,
) -> None:
    """Applying a facet narrows the crawl, so matching 'Campus Operations' (janitorial)
    would hide the real interns. Loose words only count for workerSubType."""
    facets = [
        {
            "facetParameter": "jobFamilyGroup",
            "values": [
                {"id": "campus-ops", "descriptor": "Campus Operations", "count": 14},
                {"id": "students", "descriptor": "Office/Campus Management, Custodial", "count": 9},
            ],
        }
    ]
    route = respx.post(JOBS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(20, total=25, facets=facets)),
            httpx.Response(200, json=page(5, total=25)),
        ]
    )
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(), client)

    assert all(b["appliedFacets"] == {} for b in bodies(route))
    assert len(result.postings) == 25


@respx.mock
async def test_worker_sub_type_wins_over_other_facets(settings: Settings) -> None:
    """Workday ANDs applied facets, so exactly one parameter may be used; the most
    trustworthy one wins."""
    facets = [
        {
            "facetParameter": "jobFamilyGroup",
            "values": [{"id": "fam-intern", "descriptor": "Internship", "count": 5}],
        },
        {
            "facetParameter": "workerSubType",
            "values": [{"id": "sub-intern", "descriptor": "Intern (Fixed Term)", "count": 5}],
        },
    ]
    route = respx.post(JOBS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(20, total=200, facets=facets)),
            httpx.Response(200, json=page(5, total=5)),
        ]
    )
    async with PoliteClient(settings) as client:
        await WorkdayFetcher().fetch(ref(), client)

    assert bodies(route)[1]["appliedFacets"] == {"workerSubType": ["sub-intern"]}


@respx.mock
async def test_no_intern_facet_falls_back_to_an_unfaceted_crawl(settings: Settings) -> None:
    """A board that files interns under 'Regular Employee' must still be crawled, not
    silently reported as empty."""
    only_regular = [
        {
            "facetParameter": "workerSubType",
            "values": [{"id": "regular-id", "descriptor": "Regular Employee", "count": 40}],
        }
    ]
    route = respx.post(JOBS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(20, total=25, facets=only_regular)),
            httpx.Response(200, json=page(5, total=25)),
        ]
    )
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(), client)

    # The probe response is reused as page 0 rather than re-requested.
    assert route.call_count == 2
    assert [b["offset"] for b in bodies(route)] == [0, 20]
    assert all(b["appliedFacets"] == {} for b in bodies(route))
    assert len(result.postings) == 25


@respx.mock
async def test_crawl_stops_at_the_reported_total(settings: Settings) -> None:
    """A full final page must not provoke a pointless empty request."""
    route = respx.post(JOBS_URL).mock(return_value=httpx.Response(200, json=page(20, total=20)))
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(worker_sub_types=["x"]), client)

    assert route.call_count == 1
    assert len(result.postings) == 20


@respx.mock
async def test_later_pages_reporting_zero_total_do_not_truncate(settings: Settings) -> None:
    """Regression: with a facet applied, the live API reports the real total only on the
    first page and `total: 0` afterwards. Trusting the later value stopped the crawl at
    40 of NVIDIA's 92 early-career postings."""
    route = respx.post(JOBS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(20, total=45)),
            httpx.Response(200, json=page(20, total=0)),
            httpx.Response(200, json=page(5, total=0)),
        ]
    )
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(worker_sub_types=["x"]), client)

    assert route.call_count == 3
    assert len(result.postings) == 45


@respx.mock
async def test_max_pages_caps_a_runaway_board(settings: Settings) -> None:
    route = respx.post(JOBS_URL).mock(
        return_value=httpx.Response(200, json=page(20, total=100_000))
    )
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(worker_sub_types=["x"], max_pages=3), client)

    assert route.call_count == 3
    assert len(result.postings) == 60


@respx.mock
async def test_postings_carry_the_base_url_for_the_adapter(settings: Settings) -> None:
    respx.post(JOBS_URL).mock(return_value=httpx.Response(200, json=page(1, total=1)))
    async with PoliteClient(settings) as client:
        result = await WorkdayFetcher().fetch(ref(worker_sub_types=["x"]), client)

    assert result.postings[0].payload["_base_url"] == CAREERS_URL


@respx.mock
async def test_http_error_becomes_a_fetch_error(settings: Settings) -> None:
    respx.post(JOBS_URL).mock(return_value=httpx.Response(400))
    async with PoliteClient(settings) as client:
        with pytest.raises(FetchError) as excinfo:
            await WorkdayFetcher().fetch(ref(), client)
    assert excinfo.value.http_status == 400


@respx.mock
async def test_a_200_challenge_page_reports_what_arrived(settings: Settings) -> None:
    """Bot mitigation answers 200 with HTML. The error has to name that, or a fleet-wide
    block reads as an unexplained JSONDecodeError on every board at once."""
    respx.post(JOBS_URL).mock(
        return_value=httpx.Response(
            200,
            html="<html><head><title>Just a moment...</title></head><body>checking</body></html>",
        )
    )
    async with PoliteClient(settings) as client:
        with pytest.raises(FetchError) as excinfo:
            await WorkdayFetcher().fetch(ref(), client)

    message = str(excinfo.value)
    assert "not JSON" in message
    assert "text/html" in message
    assert "Just a moment" in message
    assert excinfo.value.http_status == 200
