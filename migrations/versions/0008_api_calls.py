"""api_calls: the cost of every model call

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09

Every image call (portrait, cover, page, retry, plate) and every review call
gets one row, with the provider, the model, what it was for and what it cost.
The average cost of a book is summed from these rows, never estimated.

Nullability matches `app/db.py` column for column; the migration test builds
this schema and compares it to the models.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_calls",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("generation_id", sa.String(length=64), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("images", sa.Integer(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("cost_microusd", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_calls_book_id", "api_calls", ["book_id"], unique=False)
    op.create_index("ix_api_calls_generation_id", "api_calls", ["generation_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_calls_generation_id", table_name="api_calls")
    op.drop_index("ix_api_calls_book_id", table_name="api_calls")
    op.drop_table("api_calls")
