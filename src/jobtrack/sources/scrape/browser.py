"""Careers-page scraping fetcher — interface reserved, implementation post-v1.

The plan: a Playwright service joins docker-compose, this fetcher drives it (respecting
`config.needs_browser`, `config.proxy_pool`, robots.txt via PoliteClient.robots_allowed,
and slower per-source politeness), and the generic fallback adapter maps extracted items.
"""

from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.polite_http import FetchError, PoliteClient


class BrowserFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        raise FetchError(
            f"source {source.kind}/{source.name}: careers-page scraping is not implemented yet"
        )
