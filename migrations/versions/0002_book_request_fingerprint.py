"""books.request_fingerprint

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the hash that makes `POST /api/book/start` idempotent.

    Nullable on purpose: every book already in the table was started before
    this column existed, and a NULL fingerprint simply means "no request to
    match", so those rows are never a reuse candidate. The index is what the
    lookup actually uses — it matches on fingerprint, timestamp and status.
    """
    op.add_column(
        "books", sa.Column("request_fingerprint", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "ix_books_request_fingerprint", "books", ["request_fingerprint"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_books_request_fingerprint", table_name="books")
    op.drop_column("books", "request_fingerprint")
