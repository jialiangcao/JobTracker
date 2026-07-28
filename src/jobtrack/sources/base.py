"""Fetcher protocol and the ORM-decoupled source view fetchers receive."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from jobtrack.schema import RawPosting
from jobtrack.sources.polite_http import PoliteClient, Politeness


@dataclass(frozen=True)
class SourceRef:
    """Read-only view of a sources row, safe to hand to concurrent fetch tasks."""

    id: int
    kind: str
    name: str
    config: dict[str, Any]

    def politeness(self) -> Politeness | None:
        raw = self.config.get("politeness")
        if not isinstance(raw, dict):
            return None
        p: dict[str, Any] = raw
        return Politeness(
            max_concurrency=int(p.get("max_concurrency", 0)) or None,
            min_delay_ms=int(p["min_delay_ms"]) if "min_delay_ms" in p else None,
            max_delay_ms=int(p["max_delay_ms"]) if "max_delay_ms" in p else None,
        )

    def conditional_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if etag := self.config.get("etag"):
            headers["If-None-Match"] = str(etag)
        if last_modified := self.config.get("last_modified"):
            headers["If-Modified-Since"] = str(last_modified)
        return headers


@dataclass
class FetchResult:
    postings: list[RawPosting] = field(default_factory=list)
    http_status: int | None = None
    not_modified: bool = False
    # New validators to persist into sources.config for the next run's conditional request.
    etag: str | None = None
    last_modified: str | None = None


class Fetcher(Protocol):
    async def fetch(self, source: SourceRef, client: PoliteClient) -> FetchResult: ...
