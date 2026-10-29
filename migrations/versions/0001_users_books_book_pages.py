"""users, books, book_pages

Revision ID: 0001
Revises:
Create Date: 2026-10-29

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The three tables the app needs in database mode.

    Bytea follows the judge's ruling: books are a few MB and a second cloud
    vendor was explicitly not wanted. The `books` row carries its portrait
    and PDF; finished page art lives in `book_pages`, one row per page,
    keyed (book, page number).

    Nullability matches `app/db.py` column for column, including the four
    user columns and the two book timestamps whose values the ORM always
    supplies (`""` and `now()`). A schema that is merely "close enough" is
    one the tests never exercise: `tests/test_migrations.py` builds the schema
    with this migration and fails on any difference.
    """
    op.create_table(
        "users",
        sa.Column("sub", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("avatar", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("sub"),
    )
    op.create_table(
        "books",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("generation_id", sa.String(length=64), nullable=False),
        sa.Column("about", sa.JSON(), nullable=False),
        sa.Column("prompts", sa.JSON(), nullable=True),
        sa.Column("status_payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("portrait", sa.LargeBinary(), nullable=True),
        sa.Column("pdf", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.sub"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "book_pages",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"]),
        sa.PrimaryKeyConstraint("book_id", "page_number"),
    )


def downgrade() -> None:
    op.drop_table("book_pages")
    op.drop_table("books")
    op.drop_table("users")
