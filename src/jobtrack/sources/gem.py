"""Gem job boards: one public GraphQL call per board.

Gem serves an empty SPA shell at jobs.gem.com/<slug> and loads postings from
POST /api/public/graphql. Two things make that endpoint pleasant to use:

- `boardId` takes the board *slug* from the URL, not the board UUID that the page
  embeds in window.__GEM_TRACKING_CONTEXT__ (passing the UUID returns an empty list),
  so no bootstrap request is needed to resolve a board;
- the list query accepts `descriptionHtml`, so a whole board — descriptions included —
  arrives in a single request with no per-job detail fetch.

Schema introspection is disabled on this endpoint, so the query below was read out of
Gem's own `jobBoards` bundle (query JobBoardList) and is trimmed to the fields the
adapter uses. If Gem renames a field the response comes back with `errors` and the
fetcher raises rather than silently reporting an empty board.
"""

from typing import Any

from jobtrack.sources.base import FetchResult, SourceRef
from jobtrack.sources.common import require_slug, to_postings
from jobtrack.sources.polite_http import FetchError, PoliteClient

GRAPHQL_URL = "https://jobs.gem.com/api/public/graphql"

# Field set trimmed from Gem's own JobBoardList query. `firstPublishedTsSec` is the only
# timestamp the public board exposes; `startDateTs` is null on every board seen so far.
_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      descriptionHtml
      firstPublishedTsSec
      locations { id name city isoCountry isRemote }
      job { id department { id name } locationType employmentType }
    }
  }
}
"""


class GemFetcher:
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult:
        slug = require_slug(source)
        response = await client.post(
            GRAPHQL_URL,
            json={"query": _QUERY, "variables": {"boardId": slug}},
            headers={"Accept": "application/json"},
            politeness=source.politeness(),
        )
        if response.status_code != 200:
            raise FetchError(
                f"POST {GRAPHQL_URL} ({slug}) returned HTTP {response.status_code}",
                response.status_code,
            )
        body = response.json()
        if not isinstance(body, dict):
            raise FetchError(f"gem board {slug}: non-object GraphQL body")

        # GraphQL answers 200 with an `errors` array. Treating that as an empty board
        # would let a renamed field disable a source silently instead of tripping the
        # circuit breaker, so it is a fetch failure.
        if errors := body.get("errors"):
            messages = "; ".join(
                str(e.get("message", e)) for e in errors if isinstance(e, dict)
            ) or str(errors)
            raise FetchError(f"gem board {slug}: GraphQL errors: {messages}")

        postings = _postings(body.get("data"))
        return FetchResult(
            postings=to_postings(source, [_with_board_slug(p, slug) for p in postings]),
            http_status=response.status_code,
        )


def _postings(data: Any) -> list[Any]:
    if not isinstance(data, dict):
        return []
    board = data.get("oatsExternalJobPostings")
    if not isinstance(board, dict):
        return []
    items = board.get("jobPostings")
    return items if isinstance(items, list) else []


def _with_board_slug(item: Any, slug: str) -> Any:
    """The payload carries no apply URL — it is built from the slug and extId. RawPosting
    has no source config, so the slug rides along in the payload (as Workday does with
    its base URL)."""
    if not isinstance(item, dict):
        return item
    return item | {"_board_slug": slug}
