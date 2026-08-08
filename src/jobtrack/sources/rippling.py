"""Rippling ATS boards: unauthenticated JSON list per board.

GET /platform/api/ats/v1/board/<slug>/jobs returns a bare JSON *array* of postings. The
slug is the first path segment of the board URL (ats.rippling.com/<slug>), and boards
served on a vanity host (<slug>.rippling-ats.com) use that same slug against the API.

Deliberately list-only: the array carries title, url, department and work location but no
description, which lives behind a per-job detail call. Every seeded filter rule matches on
`title` (see filtering/rules.SEED_RULES — descriptions were dropped as pure noise), so
fetching descriptions would mean one extra request per posting every 30 minutes to feed a
field nothing reads. Discord embeds show no snippet for these sources as a result; if that
ever matters, fetch details for the jobs that survive filtering, not all of them.
"""

from typing import Any

from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import get_json, require_slug, result_from, to_postings
from jobtrack.sources.polite_http import PoliteClient

API_BASE = "https://api.rippling.com/platform/api/ats/v1/board"


class RipplingFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        slug = require_slug(source)
        url = f"{API_BASE}/{slug}/jobs"
        data, response = await get_json(source, client, url)
        if data is None:  # 304
            return result_from(response, [])
        jobs: list[Any] = data if isinstance(data, list) else []
        return result_from(response, to_postings(source, jobs))
