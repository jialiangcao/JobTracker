"""Lever postings API: unauthenticated JSON list per company."""

from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import get_json, require_slug, result_from, to_postings
from jobtrack.sources.polite_http import PoliteClient


class LeverFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        slug = require_slug(source)
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        data, response = await get_json(source, client, url)
        if data is None:  # 304
            return result_from(response, [])
        items = data if isinstance(data, list) else []
        return result_from(response, to_postings(source, items))
