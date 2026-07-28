"""Internal job schema and the raw fetcher→adapter boundary type."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class RawPosting(BaseModel):
    """One raw item as returned by a source, before adaptation."""

    model_config = ConfigDict(frozen=True)

    source_id: int
    source_kind: str
    source_name: str  # company / board display name; adapters use it when payloads lack one
    payload: dict[str, Any]
    fetched_at: datetime


class JobPosting(BaseModel):
    """Normalized internal representation every adapter maps into."""

    external_id: str | None = None  # ATS job id when available
    title: str
    company: str
    url: str  # canonical apply/detail URL
    location: str | None = None
    remote: bool | None = None
    description: str | None = None  # plain text (HTML stripped) — regex runs against this
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    department: str | None = None
    employment_type: str | None = None
    compensation: str | None = None
    source_kind: str
    raw: dict[str, Any]  # original payload, persisted with final candidates
