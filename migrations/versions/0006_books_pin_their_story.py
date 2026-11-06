"""books pin their story's words and looks; the bible carries every look; the start gate

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The story is what a book is made from, and the book says exactly which.

    * `book_story_pages` / `book_story_characters` — the page and bible
      revisions one book is drawn and printed from. Written once when the book
      starts, from its template version when there is one and from the latest
      revisions otherwise, so a changed seed or an operator's new look never
      reaches a book already made. RESTRICT on the revision: a pinned revision
      cannot be deleted (none ever is — revisions are append-only).
    * `story_characters` gains `look_with_photo`, `portrait`, `costume` and
      `position`: the bible is the one place a look is written (T4.3), so the
      photographed variant, the portrait brief and the star's story costume
      live beside the plain look, and `position` keeps the cast in the order
      the studio offers it.
    * `stories` gains `names` (the star's default name per photo-gender
      answer) and `requires_template` — the operator's start gate (T6.5).
      Off by default, so turning it on is a decision, not a migration.
    * `books` gains `title`: the resolved cover title ("Noa and the Wolf").
      Existing books are all Jack's, so they are backfilled from the name they
      were made with.

    Book pins for existing books are written at boot, after the seeds are
    loaded (`TemplateStore.pin_unpinned_books`): a migration cannot assume the
    story rows exist yet.
    """
    with op.batch_alter_table("story_characters") as batch:
        batch.add_column(sa.Column("look_with_photo", sa.Text(), nullable=True))
        batch.add_column(sa.Column("portrait", sa.Text(), nullable=True))
        batch.add_column(sa.Column("costume", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("position", sa.Integer(), nullable=False, server_default="0")
        )

    with op.batch_alter_table("stories") as batch:
        batch.add_column(sa.Column("names", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column(
                "requires_template", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )

    with op.batch_alter_table("books") as batch:
        batch.add_column(
            sa.Column("title", sa.String(length=255), nullable=False, server_default="")
        )

    op.create_table(
        "book_story_pages",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("story_page_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["story_page_id"], ["story_pages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("book_id", "page_number"),
    )
    op.create_table(
        "book_story_characters",
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("cast_key", sa.String(length=64), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["character_id"], ["story_characters.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("book_id", "cast_key"),
    )

    _backfill_titles()


def _backfill_titles() -> None:
    """Every book made before this revision is a Jack book, titled by its name."""
    conn = op.get_bind()
    books = sa.table(
        "books",
        sa.column("id", sa.String()),
        sa.column("about", sa.JSON()),
        sa.column("title", sa.String()),
    )
    for book_id, about in conn.execute(sa.select(books.c.id, books.c.about)).all():
        fields = json.loads(about) if isinstance(about, str) else (about or {})
        name = str(fields.get("prompt_name") or "Jack")
        conn.execute(
            sa.update(books)
            .where(books.c.id == book_id)
            .values(title=f"{name} and the Beanstalk")
        )


def downgrade() -> None:
    op.drop_table("book_story_characters")
    op.drop_table("book_story_pages")
    with op.batch_alter_table("books") as batch:
        batch.drop_column("title")
    with op.batch_alter_table("stories") as batch:
        batch.drop_column("requires_template")
        batch.drop_column("names")
    with op.batch_alter_table("story_characters") as batch:
        batch.drop_column("position")
        batch.drop_column("costume")
        batch.drop_column("portrait")
        batch.drop_column("look_with_photo")
