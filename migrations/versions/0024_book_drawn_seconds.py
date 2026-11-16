"""How long a whole book took to draw, for the progress estimate

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-16

Audit 11-15, other implementations A9: a new book says about how long it has left,
from the median of the last 30 whole books. `drawn_seconds` is the time from
the job starting to its PDF being written, kept only for a fresh book drawn in
one go (not an edit, a "finish this book" or a resume after a restart).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision: str | None = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(sa.Column("drawn_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.drop_column("drawn_seconds")
