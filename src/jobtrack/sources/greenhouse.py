"""Greenhouse job board API: one unauthenticated call returns all jobs with content."""

from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import get_json, require_slug, result_from, to_postings
from jobtrack.sources.polite_http import PoliteClient


class GreenhouseFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        slug = require_slug(source)
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        data, response = await get_json(source, client, url)
        if data is None:  # 304
            return result_from(response, [])
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return result_from(response, to_postings(source, jobs))
