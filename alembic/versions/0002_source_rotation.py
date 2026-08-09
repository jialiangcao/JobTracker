"""add sources.last_polled_at for per-run rotation

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Left NULL on existing rows on purpose: never-polled sorts first, so the first run
    # after this migration sees the whole fleet as due and the budget slices it by id.
    op.add_column("sources", sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True))
    # The rotation query orders every run by this column; partial index because disabled
    # sources are never selected and there are thousands of them.
    op.create_index(
        "ix_sources_rotation",
        "sources",
        ["last_polled_at", "id"],
        postgresql_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index("ix_sources_rotation", table_name="sources")
    op.drop_column("sources", "last_polled_at")
