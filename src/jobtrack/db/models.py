"""SQLAlchemy ORM models. JSON columns use JSONB on Postgres, plain JSON elsewhere (tests)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONVariant = JSON().with_variant(postgresql.JSONB(), "postgresql")
# BigInteger PKs on Postgres; SQLite needs plain Integer for autoincrement in tests.
PKInteger = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_sources_kind_name"),
        # Serves the rotation ordering in repo.get_enabled_sources; partial on Postgres
        # because disabled sources are never selected (the kwarg is ignored on SQLite).
        Index(
            "ix_sources_rotation",
            "last_polled_at",
            "id",
            postgresql_where=text("enabled"),
        ),
    )

    id: Mapped[int] = mapped_column(PKInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text)  # greenhouse | lever | ashby | ... | scrape | board
    name: Mapped[str] = mapped_column(Text)  # display name, e.g. company
    # {slug | url, adapter_override?, politeness?: {max_concurrency, min_delay_ms, max_delay_ms},
    #  needs_browser?, proxy_pool?, etag?, last_modified?}
    config: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(Text, default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Rotation cursor: advanced on every poll attempt, success or failure. Ordering by it
    # (nulls first) is what makes a per-run budget fair — see repo.get_enabled_sources.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(PKInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(Text, default="running")  # running|success|partial|failed
    sources_total: Mapped[int | None] = mapped_column(Integer, default=None)
    sources_ok: Mapped[int | None] = mapped_column(Integer, default=None)
    sources_failed: Mapped[int | None] = mapped_column(Integer, default=None)
    jobs_fetched: Mapped[int | None] = mapped_column(Integer, default=None)
    jobs_matched: Mapped[int | None] = mapped_column(Integer, default=None)
    jobs_new: Mapped[int | None] = mapped_column(Integer, default=None)
    jobs_sent: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)  # run-level failure detail


class RunSourceResult(Base):
    __tablename__ = "run_source_results"
    __table_args__ = (Index("ix_run_source_results_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(PKInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    status: Mapped[str] = mapped_column(Text)  # ok | error | skipped
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    fetched_count: Mapped[int | None] = mapped_column(Integer, default=None)
    matched_count: Mapped[int | None] = mapped_column(Integer, default=None)
    new_count: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class SeenJob(Base):
    """The dedup list; retained indefinitely (tiny data)."""

    __tablename__ = "seen_jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_seen_jobs_source_external"),
        UniqueConstraint("source_id", "canonical_url", name="uq_seen_jobs_source_url"),
        Index("ix_seen_jobs_source_hash", "source_id", "content_hash"),
    )

    id: Mapped[int] = mapped_column(PKInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str | None] = mapped_column(Text, default=None)  # dedup key #1
    canonical_url: Mapped[str] = mapped_column(Text)  # dedup key #2 (normalized)
    content_hash: Mapped[str] = mapped_column(Text)  # sha256(title|company|location), key #3
    matched: Mapped[bool] = mapped_column(Boolean)  # passed filters when first seen
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), default=None)


class Candidate(Base):
    """Final matches; doubles as the Discord send outbox."""

    __tablename__ = "candidates"
    __table_args__ = (Index("ix_candidates_send_status", "send_status"),)

    id: Mapped[int] = mapped_column(PKInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    seen_job_id: Mapped[int] = mapped_column(ForeignKey("seen_jobs.id"))
    title: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text, default=None)
    url: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONVariant)  # full original payload
    send_status: Mapped[str] = mapped_column(Text, default="pending")  # pending | sent | failed
    send_attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    discord_message_id: Mapped[str | None] = mapped_column(Text, default=None)


class FilterRule(Base):
    __tablename__ = "filter_rules"

    id: Mapped[int] = mapped_column(PKInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text)  # "group" or "group:variant"; groups are AND'd
    kind: Mapped[str] = mapped_column(Text)  # include | exclude
    field: Mapped[str] = mapped_column(Text)  # title | description | location | title+description
    pattern: Mapped[str] = mapped_column(Text)  # Python regex, compiled with re.IGNORECASE
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
