"""Workday CxS search API: paginated JSON per tenant, addressed by careers-page URL.

Three things make this fetcher longer than the other ATS ones:

- the listing endpoint is POST (the query lives in the body), so no conditional requests;
- a board is addressed by host + tenant + site rather than one slug;
- `limit` is capped at 20 server-side, so a 2000-posting tenant is 100 requests unless
  the crawl is narrowed. The `workerSubType` facet is that narrowing — see `_intern_facets`.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from jobtrack.observability.logging import get_logger
from jobtrack.schema import RawPosting
from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import to_postings
from jobtrack.sources.polite_http import FetchError, PoliteClient

log = get_logger(__name__)

_PAGE_SIZE = 20  # server-side hard cap; limit=50 answers HTTP 400
_MAX_PAGES = 25  # safety valve for boards with no usable intern facet (500 postings)
_DEFAULT_LOCALE = "en-US"
_LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")

# Only ~40% of boards classify interns under workerSubType. The rest scatter them across
# jobFamilyGroup, jobFamily and a long tail of tenant-defined parameters, so facet
# discovery scans every parameter — but with two different vocabularies.
#
# workerSubType values are employment classes ("Intern (Fixed Term)", "New College
# Graduate", "Regular Employee"), so loose matching there is safe.
_SUBTYPE_WORDS = re.compile(
    r"intern|co[-\s]?op|apprentice|college\s+grad|student|campus|trainee|placement",
    re.IGNORECASE,
)
# Every other parameter describes what the job *is*, where the same loose words are traps:
# "Campus Operations" and "Office/Campus Management, Custodial" are janitorial families.
# Applying a facet narrows the crawl, so a wrong match hides the interns entirely — hence
# only unambiguous early-career phrasing counts here.
_EARLY_CAREER = re.compile(
    r"\binterns?\b|\binternships?\b|\bco-?ops?\b|\bapprentice(?:ship)?s?\b|\bearly career",
    re.IGNORECASE,
)
# Most trustworthy first: a board that answers on workerSubType should use it. One facet
# parameter is applied at a time — Workday ANDs them, so combining would over-narrow.
_FACET_PREFERENCE = ("workerSubType", "jobFamilyGroup", "jobFamily")


class WorkdayCoordinates:
    """The three parts of a Workday board address, derived from its careers-page URL."""

    def __init__(self, host: str, tenant: str, site: str, locale: str) -> None:
        self.host = host
        self.tenant = tenant
        self.site = site
        self.locale = locale

    @property
    def jobs_url(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    @property
    def base_url(self) -> str:
        """Public job-page prefix; the adapter appends each posting's externalPath."""
        return f"https://{self.host}/{self.locale}/{self.site}"


def coordinates(source: SourceRef) -> WorkdayCoordinates:
    """Parse config.url, e.g. https://nvidia.wd5.myworkdayjobs.com/en-US/ExternalCareerSite.

    The tenant is usually the first host label and the site the last path segment, but
    neither is guaranteed, so config.tenant and config.site override the derivation.
    """
    url = source.config.get("url")
    where = f"source {source.kind}/{source.name}"
    if not isinstance(url, str) or not url:
        raise FetchError(f"{where}: config.url missing")

    parts = urlsplit(url if "//" in url else f"https://{url}")
    host = parts.netloc.lower()
    if not host:
        raise FetchError(f"{where}: config.url {url!r} has no host")

    segments = [s for s in parts.path.split("/") if s]
    locale = next((s for s in segments if _LOCALE.match(s)), _DEFAULT_LOCALE)
    site_segments = [s for s in segments if not _LOCALE.match(s)]

    tenant = source.config.get("tenant") or host.split(".", 1)[0]
    site = source.config.get("site") or (site_segments[-1] if site_segments else None)
    if not isinstance(tenant, str) or not tenant:
        raise FetchError(f"{where}: could not derive a tenant from {url!r}")
    if not isinstance(site, str) or not site:
        raise FetchError(f"{where}: could not derive a site from {url!r}; set config.site")

    return WorkdayCoordinates(host=host, tenant=tenant, site=site, locale=locale)


def _early_career_facet(facets: Any) -> tuple[str, list[str]] | None:
    """Find one facet parameter whose values read as early-career, and its ids.

    The ids are per-tenant GUIDs, so they cannot be hardcoded — but every response ships
    them next to a human descriptor, which is what the regexes match. Returning None means
    "this board doesn't classify interns in any way we recognize", and the caller falls
    back to a bounded unfaceted crawl rather than reporting an empty board.
    """
    if not isinstance(facets, list):
        return None

    matched: dict[str, list[str]] = {}
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        param = facet.get("facetParameter")
        values = facet.get("values")
        if not isinstance(param, str) or not isinstance(values, list):
            continue
        pattern = _SUBTYPE_WORDS if param == "workerSubType" else _EARLY_CAREER
        ids = [
            value["id"]
            for value in values
            if isinstance(value, dict)
            and isinstance(value.get("id"), str)
            and isinstance(value.get("descriptor"), str)
            and pattern.search(value["descriptor"])
        ]
        if ids:
            matched.setdefault(param, ids)

    for preferred in _FACET_PREFERENCE:
        if preferred in matched:
            return preferred, matched[preferred]
    return next(iter(matched.items()), None)


def _max_pages(source: SourceRef) -> int:
    configured = source.config.get("max_pages")
    if isinstance(configured, int) and configured > 0:
        return configured
    return _MAX_PAGES


def _configured_facet(source: SourceRef) -> tuple[str, list[str]] | None:
    """config.worker_sub_types pins the workerSubType ids, skipping facet discovery."""
    configured = source.config.get("worker_sub_types")
    if not isinstance(configured, list):
        return None
    ids = [v for v in configured if isinstance(v, str) and v]
    return ("workerSubType", ids) if ids else None


class WorkdayFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        coords = coordinates(source)
        applied = _configured_facet(source)

        if applied is None:
            # Probe the unfaceted board once to read its facet ids, then discard the page:
            # re-running from offset 0 with the facet applied is one wasted request and
            # saves dozens on any board with more than a few hundred postings.
            probe, status = await self._page(source, client, coords, offset=0, facet=None)
            applied = _early_career_facet(probe.get("facets"))
            if applied is None:
                log.info(
                    "workday: no early-career facet, crawling unfaceted",
                    source=source.name,
                    total=probe.get("total"),
                )
                return await self._crawl(
                    source, client, coords, facet=None, first=probe, status=status
                )
            log.info(
                "workday: narrowing by facet",
                source=source.name,
                facet=applied[0],
                values=len(applied[1]),
            )

        return await self._crawl(source, client, coords, facet=applied)

    async def _crawl(
        self,
        source: SourceRef,
        client: PoliteClient,
        coords: WorkdayCoordinates,
        *,
        facet: tuple[str, list[str]] | None,
        first: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> FetchResult:
        """Page until a short page, the reported total, or the page cap. `first` lets the
        unfaceted path reuse the probe response instead of re-requesting offset 0."""
        postings: list[RawPosting] = []
        last_status = status
        total: int | None = None
        limit = _max_pages(source)

        for page in range(limit):
            if page == 0 and first is not None:
                data = first
            else:
                data, last_status = await self._page(
                    source, client, coords, offset=page * _PAGE_SIZE, facet=facet
                )

            # Only the first page carries a real total: with a facet applied, Workday
            # reports `total: 0` on every page after it. Re-reading that would look like
            # "we've passed the end" and truncate the board silently.
            reported = data.get("total")
            if total is None and isinstance(reported, int) and reported > 0:
                total = reported
            items = data.get("jobPostings")
            items = items if isinstance(items, list) else []
            postings.extend(to_postings(source, [self._with_base_url(i, coords) for i in items]))

            if len(items) < _PAGE_SIZE:
                break
            if total is not None and (page + 1) * _PAGE_SIZE >= total:
                break
        else:
            log.warning(
                "workday: hit the page cap, board may be truncated",
                source=source.name,
                pages=limit,
                total=total,
            )

        # POST responses carry no useful validators, so no ETag/Last-Modified round-trip.
        return FetchResult(postings=postings, http_status=last_status)

    @staticmethod
    def _with_base_url(item: Any, coords: WorkdayCoordinates) -> Any:
        """Hand the adapter the prefix it needs for externalPath. RawPosting carries no
        source config, so the one derived value the adapter needs rides in the payload."""
        if not isinstance(item, dict):
            return item
        return item | {"_base_url": coords.base_url}

    async def _page(
        self,
        source: SourceRef,
        client: PoliteClient,
        coords: WorkdayCoordinates,
        *,
        offset: int,
        facet: tuple[str, list[str]] | None,
    ) -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {
            "appliedFacets": {facet[0]: facet[1]} if facet else {},
            "limit": _PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        response = await client.post(
            coords.jobs_url,
            json=body,
            headers={"Accept": "application/json"},
            politeness=source.politeness(),
        )
        if response.status_code != 200:
            raise FetchError(
                f"POST {coords.jobs_url} returned HTTP {response.status_code}",
                response.status_code,
            )
        # Bot mitigation answers HTTP 200 with an HTML challenge page, so a status check
        # is not enough to know we got JSON. Report what actually arrived — a bare
        # JSONDecodeError here says nothing about why a thousand boards stopped working.
        try:
            data = response.json()
        except ValueError as exc:
            snippet = " ".join(response.text[:200].split())
            raise FetchError(
                f"POST {coords.jobs_url} returned HTTP {response.status_code} but not JSON "
                f"(content-type={response.headers.get('content-type', 'none')}): {snippet!r}",
                response.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise FetchError(f"POST {coords.jobs_url} returned a non-object body")
        return data, response.status_code
