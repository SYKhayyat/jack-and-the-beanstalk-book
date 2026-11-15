"""Keep a small working copy of each plate for drawing

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-15

Plates are 2388 x 1791 lossless WebP in the object store, and every book
decoded all 21 of them before page 1 could start: about 70 s of silence on the
free plan (audit 11-15 S1). `working_image` is the plate shrunk to the largest
picture a provider returns, stored as JPEG so it is read back without a
decode-and-re-encode. Existing plates get theirs the first time a page uses
them, so this revision only adds the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision: str | None = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("template_plates") as batch:
        batch.add_column(sa.Column("working_image", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("template_plates") as batch:
        batch.drop_column("working_image")
