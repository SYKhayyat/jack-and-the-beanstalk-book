"""Screen-sized pictures under versioned URLs

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-09

* `books.cover_sha256` / `books.portrait_sha256`: the hash of each picture,
  written with it, as `book_pages.image_sha256` already is. It is the version
  in the picture's URL, so a browser may keep the picture for a year and a
  redrawn one still arrives under a new address.
* `picture_variants`: the WebP copies made for the screen (a page at 800 and
  1600 px wide, a thumbnail at 240), made on first request and kept.

Existing pictures are hashed here, so every book has versioned URLs from the
first request after the deploy.

Nullability matches `app/db.py` column for column; the migration test builds
this schema and compares it to the models.
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("books", sa.Column("cover_sha256", sa.String(length=64), nullable=True))
    op.add_column("books", sa.Column("portrait_sha256", sa.String(length=64), nullable=True))
    op.create_table(
        "picture_variants",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("slot", sa.String(length=16), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("book_id", "slot", "width"),
    )
    _backfill_hashes()


def _backfill_hashes() -> None:
    """Hash every stored cover, portrait and page that has no hash yet.

    One row at a time: a book's pictures are megabytes each, and reading every
    blob in one result would hold them all in memory at once.
    """
    conn = op.get_bind()
    book_ids = [
        row[0]
        for row in conn.execute(
            sa.text("SELECT id FROM books WHERE cover IS NOT NULL OR portrait IS NOT NULL")
        )
    ]
    for book_id in book_ids:
        row = conn.execute(
            sa.text("SELECT cover, portrait FROM books WHERE id = :id"), {"id": book_id}
        ).one()
        conn.execute(
            sa.text(
                "UPDATE books SET cover_sha256 = :cover, portrait_sha256 = :portrait "
                "WHERE id = :id"
            ),
            {"id": book_id, "cover": _sha(row[0]), "portrait": _sha(row[1])},
        )
    pages = conn.execute(
        sa.text("SELECT book_id, page_number FROM book_pages WHERE image_sha256 IS NULL")
    ).all()
    for book_id, number in pages:
        image = conn.execute(
            sa.text("SELECT image FROM book_pages WHERE book_id = :id AND page_number = :n"),
            {"id": book_id, "n": number},
        ).scalar_one()
        conn.execute(
            sa.text(
                "UPDATE book_pages SET image_sha256 = :sha "
                "WHERE book_id = :id AND page_number = :n"
            ),
            {"id": book_id, "n": number, "sha": _sha(image)},
        )


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(bytes(data)).hexdigest() if data is not None else None


def downgrade() -> None:
    op.drop_table("picture_variants")
    with op.batch_alter_table("books") as batch:
        batch.drop_column("portrait_sha256")
        batch.drop_column("cover_sha256")
