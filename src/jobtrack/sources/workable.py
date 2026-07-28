"""Workable public widget API: unauthenticated JSON per account."""

from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import get_json, require_slug, result_from, to_postings
from jobtrack.sources.polite_http import PoliteClient


class WorkableFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        slug = require_slug(source)
        url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
        data, response = await get_json(source, client, url)
        if data is None:  # 304
            return result_from(response, [])
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return result_from(response, to_postings(source, jobs))
