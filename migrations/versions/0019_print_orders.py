"""Record a printed copy: the owner's approval, the address, the test job

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-13

A print order is a separately persisted thing from a book because it has its
own facts: which exact PDF was approved, where it goes, what it cost, and what
the print service answered. A book can be edited after an order; the approval's
`pdf_sha256` is what makes that visible instead of printing a different book.

The table starts empty, so every existing database migrates without touching a
book. `provider` is stored per row rather than read from settings, so an order
placed by the mock provider still says so after a deployment switches to
Peecho's test environment.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "print_orders",
        sa.Column("id", sa.String(length=48), primary_key=True),
        sa.Column(
            "book_id",
            sa.String(length=64),
            sa.ForeignKey("books.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("printing_cents", sa.Integer(), nullable=False),
        sa.Column("shipping_cents", sa.Integer(), nullable=False),
        sa.Column("retail_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "provider_order_id", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column("address", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_print_orders_book_id", "print_orders", ["book_id"])
    op.create_index("ix_print_orders_user_id", "print_orders", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_print_orders_user_id", table_name="print_orders")
    op.drop_index("ix_print_orders_book_id", table_name="print_orders")
    op.drop_table("print_orders")
