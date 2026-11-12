"""Give every render a lease, so a second worker can tell a live job from a dead one

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-11

`BookRunner._active` and `_ensure_idle` are in-process: two Render instances (or
two Uvicorn workers) would each believe they were the only one rendering a
book, and the boot `resume_incomplete` would pick up a book another live
instance was still drawing — paying for the same pages twice.

These two columns move that one fact into the database:

* `job_owner` is the worker holding the book (`host:pid:random`), and
* `job_expires_at` is when its claim runs out — renewed by a heartbeat while
  the job runs, so a long render keeps it and a killed process loses it.

A start claims the row with a conditional `UPDATE`; the boot resume only takes
books whose lease is free or expired. Both nullable, so every existing book
migrates without a rewrite (and starts out claimable).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(sa.Column("job_owner", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("job_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.drop_column("job_expires_at")
        batch.drop_column("job_owner")
