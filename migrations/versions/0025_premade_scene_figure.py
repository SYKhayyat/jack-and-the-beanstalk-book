"""Where the stand-in child is on a pre-made picture of a star page

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-16

Audit 11-15 friends' repos A5: a pre-made picture may now be of a page the
star is in, painted once with a stand-in child; `figure` is that child's box
(x, y, width, height as shares of the picture). A book cuts out only that box,
has the image model turn the stand-in into its own child, and blends it back.
Empty on a page the star is not in, which is used whole as before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision: str | None = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("template_premade_pages") as batch:
        batch.add_column(sa.Column("figure", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("template_premade_pages") as batch:
        batch.drop_column("figure")
