"""The page shape a book is set in

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-17

A book is square (8.5 x 8.5), tall (8.5 x 11) or wide (11 x 8.5, each
picture facing its words). The owner picks it when making the book and may
change it in the viewer; the reader, the PDF and the print order follow it.
Every book made before this is square, the shape the print page offered first.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(
            sa.Column("book_format", sa.String(16), nullable=False, server_default="square")
        )


def downgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.drop_column("book_format")
