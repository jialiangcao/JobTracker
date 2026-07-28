"""SmartRecruiters public postings API: paginated JSON per company."""

from typing import Any

from jobtrack.schema import RawPosting
from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import get_json, require_slug, to_postings
from jobtrack.sources.polite_http import PoliteClient

_PAGE_SIZE = 100
_MAX_PAGES = 10  # safety valve; 1000 postings is far beyond any board we track


class SmartRecruitersFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        slug = require_slug(source)
        postings: list[RawPosting] = []
        last_status: int | None = None
        for page in range(_MAX_PAGES):
            url = (
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                f"?limit={_PAGE_SIZE}&offset={page * _PAGE_SIZE}"
            )
            # Conditional headers only make sense for the single-request fetchers.
            data, response = await get_json(source, client, url, conditional=False)
            last_status = response.status_code
            content: list[Any] = data.get("content", []) if isinstance(data, dict) else []
            postings.extend(to_postings(source, content))
            if len(content) < _PAGE_SIZE:
                break
        return FetchResult(postings=postings, http_status=last_status)
