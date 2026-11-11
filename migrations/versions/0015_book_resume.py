"""Let a restart resume the book it interrupted instead of failing it

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-11

Run 2 on the live site lost 14 finished pages (about $0.36) when the free
instance restarted mid-render: `reap_incomplete` had no way to pick the book
up, so it marked it failed. These two columns make that decision honest:

* `job_started_at` is when the render first started. A resume does not move it,
  so the window (`BOOK_RESUME_WINDOW_MINUTES`) stays measured from the real
  start and repeated restarts cannot extend it forever.
* `resume_count` counts how many times a restart resumed the book. It is
  bounded (`BOOK_MAX_RESUMES`), so a book that is what kills the server cannot
  keep killing it; past the bound it fails and its owner can press "Finish this
  book".

Both are nullable/defaulted so every existing book migrates without a rewrite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(sa.Column("job_started_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column(
                "resume_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.drop_column("resume_count")
        batch.drop_column("job_started_at")
