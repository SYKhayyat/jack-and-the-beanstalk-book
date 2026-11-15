"""Remember whether a book is being shared, so the page can say so

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-15

A share link is a signed token with no row behind it (0020), so the book page
could only show a link while its own URL carried one. After "Stop sharing" the
owner had nothing to tell them sharing was off (audit 11-15 R3). `sharing_on`
is set when a link is made and cleared when sharing stops; the page shows the
live link, rebuilt from the book's generation, only while it is set.

Existing books start with it off: a link sent before this revision keeps
working until it expires, as before, and making a link turns the flag on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision: str | None = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(
            sa.Column("sharing_on", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.drop_column("sharing_on")
