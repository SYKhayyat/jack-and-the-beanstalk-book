"""stories.cover_subtitle: a small line under the cover title

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09

A tale that keeps its own name on the cover (Little Red Riding Hood) names the
star in a line beneath it instead ("starring {name}"). Empty for a story whose
title already carries the name.

Nullability matches `app/db.py` column for column; the migration test builds
this schema and compares it to the models.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column("cover_subtitle", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("stories", "cover_subtitle")
