"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("kind", "name", name="uq_sources_kind_name"),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("sources_total", sa.Integer(), nullable=True),
        sa.Column("sources_ok", sa.Integer(), nullable=True),
        sa.Column("sources_failed", sa.Integer(), nullable=True),
        sa.Column("jobs_fetched", sa.Integer(), nullable=True),
        sa.Column("jobs_matched", sa.Integer(), nullable=True),
        sa.Column("jobs_new", sa.Integer(), nullable=True),
        sa.Column("jobs_sent", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "run_source_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("fetched_count", sa.Integer(), nullable=True),
        sa.Column("matched_count", sa.Integer(), nullable=True),
        sa.Column("new_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_run_source_results_run_id", "run_source_results", ["run_id"])

    op.create_table(
        "seen_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_run_id", sa.BigInteger(), sa.ForeignKey("runs.id"), nullable=True),
        sa.UniqueConstraint("source_id", "external_id", name="uq_seen_jobs_source_external"),
        sa.UniqueConstraint("source_id", "canonical_url", name="uq_seen_jobs_source_url"),
    )
    op.create_index("ix_seen_jobs_source_hash", "seen_jobs", ["source_id", "content_hash"])

    op.create_table(
        "candidates",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("seen_job_id", sa.BigInteger(), sa.ForeignKey("seen_jobs.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("send_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("send_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discord_message_id", sa.Text(), nullable=True),
    )
    op.create_index("ix_candidates_send_status", "candidates", ["send_status"])

    op.create_table(
        "filter_rules",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("filter_rules")
    op.drop_index("ix_candidates_send_status", table_name="candidates")
    op.drop_table("candidates")
    op.drop_index("ix_seen_jobs_source_hash", table_name="seen_jobs")
    op.drop_table("seen_jobs")
    op.drop_index("ix_run_source_results_run_id", table_name="run_source_results")
    op.drop_table("run_source_results")
    op.drop_table("runs")
    op.drop_table("sources")
