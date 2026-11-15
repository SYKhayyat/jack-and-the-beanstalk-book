"""Print orders carry their own approved files and outlive their book

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-14

Three owner decisions land here (AUDIT.md §10):

* **Q2/A1** — an order is for one edition (`product_key`: trim and binding),
  and approval builds that edition's interior and cover once. The two files
  are stored on the row with their hashes, and the printer is served exactly
  those bytes (**A3**), not whatever the book's PDF has become since.
* **A6** — shipping is quoted for the buyer's address before they pay;
  `quoted_at` records when.
* **A9** — paid books and print orders are kept forever, so an order no longer
  cascades away with its book: the foreign key is dropped and `book_id` stays
  as a plain, indexed reference.

The table is rebuilt rather than altered so the dropped foreign key works the
same on SQLite (which cannot drop an unnamed constraint in place) and Postgres.
Existing rows become square hardcover approvals with no stored files; an
approval without files is re-approved before it can be placed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision: str | None = "0020"
branch_labels = None
depends_on = None

_KEEP = (
    "id, book_id, user_id, state, pdf_sha256, page_count, printing_cents, "
    "shipping_cents, retail_cents, currency, provider, provider_order_id, address, "
    "error, approved_at, created_at, updated_at"
)


def _columns(*, with_fk: bool, editions: bool) -> list[sa.Column[object]]:
    book_id = (
        sa.Column(
            "book_id",
            sa.String(length=64),
            sa.ForeignKey("books.id", ondelete="CASCADE"),
            nullable=False,
        )
        if with_fk
        else sa.Column("book_id", sa.String(length=64), nullable=False)
    )
    columns: list[sa.Column[object]] = [
        sa.Column("id", sa.String(length=48), primary_key=True),
        book_id,
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
    ]
    if editions:
        columns += [
            sa.Column(
                "product_key",
                sa.String(length=32),
                nullable=False,
                server_default="square-hardcover",
            ),
            sa.Column(
                "interior_sha256", sa.String(length=64), nullable=False, server_default=""
            ),
            sa.Column("cover_sha256", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("interior", sa.LargeBinary(), nullable=True),
            sa.Column("cover", sa.LargeBinary(), nullable=True),
            sa.Column("quoted_at", sa.DateTime(), nullable=True),
        ]
    return columns


def _rebuild(*, with_fk: bool, editions: bool) -> None:
    op.create_table("print_orders_next", *_columns(with_fk=with_fk, editions=editions))
    op.execute(f"INSERT INTO print_orders_next ({_KEEP}) SELECT {_KEEP} FROM print_orders")
    op.drop_index("ix_print_orders_user_id", table_name="print_orders")
    op.drop_index("ix_print_orders_book_id", table_name="print_orders")
    op.drop_table("print_orders")
    op.rename_table("print_orders_next", "print_orders")
    op.create_index("ix_print_orders_book_id", "print_orders", ["book_id"])
    op.create_index("ix_print_orders_user_id", "print_orders", ["user_id"])


def upgrade() -> None:
    _rebuild(with_fk=False, editions=True)


def downgrade() -> None:
    # Orders whose book is gone cannot point at it again.
    op.execute("DELETE FROM print_orders WHERE book_id NOT IN (SELECT id FROM books)")
    _rebuild(with_fk=True, editions=False)
