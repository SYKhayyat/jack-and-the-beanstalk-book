"""Let an owner stop a book's share links: a per-book share generation

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-14

A share link is a signed token with no row behind it, so until now the only
way to stop one was to delete the book (A10). Every new link now carries the
book's `share_generation`, and a link works only while that number still
matches. "Stop sharing" adds one, which retires every link already sent at
once without storing a list of them.

Existing books start at 0, and a link minted before this revision carries no
generation, which reads as 0: every link already sent keeps working until its
owner stops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(
            sa.Column("share_generation", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.drop_column("share_generation")
