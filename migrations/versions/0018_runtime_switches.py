"""Give the operator a switch that stops spending in the second it is pulled

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-12

A cost kill switch has to work before the next deploy, and a render already
running has to see the same answer the operator page shows, so the two facts
live in the database rather than the environment:

* `new_books` refuses a new portrait, a new book and a new edit at the door;
* `drawing` stops the jobs in flight before their next picture, leaving the
  book resumable (turning the switch back on resumes them).

A missing row means off, so the table starts empty and every existing database
migrates without a rewrite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_switches",
        sa.Column("name", sa.String(length=32), primary_key=True),
        sa.Column("on", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("changed_by", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_switches")
