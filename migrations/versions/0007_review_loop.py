"""the cover gets the same attempt record a page has

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08

Revision 0005 gave `book_pages` everything the quality loop needs — attempt
count, every verdict, which attempt was kept, and a best-effort flag — but gave
`books` only the cover's verdicts. The cover then went through the same
three-try loop as a page (T9.3) and had nowhere to say what it cost.

So these are exactly the page's three columns, on the cover: how many billable
attempts it took, which one is on the book, and whether the loop ran out of
tries without a pass.

Nothing is backfilled. A book made before this took one attempt and passed it,
which is what the defaults (0 / null / false) already say.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(
            sa.Column("cover_attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("cover_chosen_attempt", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "cover_best_effort",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.drop_column("cover_best_effort")
        batch.drop_column("cover_chosen_attempt")
        batch.drop_column("cover_attempt_count")
