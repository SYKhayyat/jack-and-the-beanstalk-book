"""books.settings, source_photo, cover; book_characters

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist what an edited book needs after the original draft expires.

    * `settings` — the closed-set art settings; a regenerated page must use the
      same aspect ratio and style as the rest of the book.
    * `source_photo` — the original photograph, the second identity anchor.
    * `cover` — the one AI cover image.
    * `book_characters` — each photographed cast member's portrait and photo,
      per book rather than per draft (drafts expire in a day).

    All book columns are nullable so every row written before this revision
    remains a valid book; the runner refuses to edit one whose anchors are
    missing rather than guessing.
    """
    op.add_column("books", sa.Column("settings", sa.JSON(), nullable=True))
    op.add_column("books", sa.Column("source_photo", sa.LargeBinary(), nullable=True))
    op.add_column("books", sa.Column("cover", sa.LargeBinary(), nullable=True))
    op.create_table(
        "book_characters",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("cast_key", sa.String(length=64), nullable=False),
        sa.Column("portrait", sa.LargeBinary(), nullable=False),
        sa.Column("portrait_media_type", sa.String(length=50), nullable=False),
        sa.Column("photo", sa.LargeBinary(), nullable=False),
        sa.Column("photo_media_type", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("book_id", "cast_key"),
    )


def downgrade() -> None:
    op.drop_table("book_characters")
    op.drop_column("books", "cover")
    op.drop_column("books", "source_photo")
    op.drop_column("books", "settings")
