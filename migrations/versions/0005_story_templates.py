"""stories, story_pages, story_characters, template plates and versions; book pins

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

DEFAULT_STORY_KEY = "jack_and_the_beanstalk"


def upgrade() -> None:
    """Stories become data, and a book pins the template it started on.

    * `stories`, `story_pages`, `story_characters` — words, scenes and the
      character bible, seeded from `app/books/*.toml`. Page and character rows
      are append-only revisions, so a pinned version keeps the old words.
    * `template_plates` — the empty art per page, draft/approved/retired.
    * `template_versions` + its two child tables — one complete set of plates,
      page revisions and bible revisions. A book pins exactly one.
    * `books` gains `story_key`, `template_version_id` (RESTRICT: a version a
      book uses cannot be deleted) and `cover_review`.
    * `book_pages` gains the quality-loop columns and `image_sha256`.

    Every `ALTER` goes through batch mode so SQLite can run it (T3). New
    NOT NULL columns on existing tables carry a server default, so every row
    written before this revision stays valid: old books become Jack books with
    no template, and old pages have zero attempts and no review.
    `image_sha256` is not backfilled; it is written with every new image and
    T13 fills the gaps lazily.
    """
    op.create_table(
        "stories",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("blurb", sa.Text(), nullable=False),
        sa.Column("book_title", sa.String(length=255), nullable=False),
        sa.Column("star", sa.String(length=64), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "story_pages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("page_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("prose", sa.Text(), nullable=False),
        sa.Column("beat", sa.Text(), nullable=False),
        sa.Column("emotion", sa.Text(), nullable=False),
        sa.Column("scene", sa.JSON(), nullable=False),
        sa.Column("cast_keys", sa.JSON(), nullable=False),
        sa.Column("text_band", sa.JSON(), nullable=False),
        sa.Column("seed_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["story_key"], ["stories.key"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "story_key", "page_number", "revision", name="uq_story_page_revision"
        ),
    )
    op.create_table(
        "story_characters",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("cast_key", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("look", sa.Text(), nullable=False),
        sa.Column("photoable", sa.Boolean(), nullable=False),
        sa.Column("reference_image", sa.LargeBinary(), nullable=True),
        sa.Column("reference_media_type", sa.String(length=50), nullable=True),
        sa.Column("provenance", sa.String(length=16), nullable=False),
        sa.Column("seed_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["story_key"], ["stories.key"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "story_key", "cast_key", "revision", name="uq_story_character_revision"
        ),
    )
    op.create_table(
        "template_plates",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("slots", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["story_key"], ["stories.key"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_template_plates_story_key", "template_plates", ["story_key"], unique=False
    )
    op.create_table(
        "template_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["story_key"], ["stories.key"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("story_key", "number", name="uq_template_version_number"),
    )
    op.create_table(
        "template_version_plates",
        sa.Column("template_version_id", sa.String(length=32), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("plate_id", sa.String(length=32), nullable=False),
        sa.Column("story_page_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_version_id"], ["template_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["plate_id"], ["template_plates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["story_page_id"], ["story_pages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("template_version_id", "page_number"),
    )
    op.create_index(
        "ix_template_version_plates_plate_id",
        "template_version_plates",
        ["plate_id"],
        unique=False,
    )
    op.create_table(
        "template_version_characters",
        sa.Column("template_version_id", sa.String(length=32), nullable=False),
        sa.Column("cast_key", sa.String(length=64), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_version_id"], ["template_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["character_id"], ["story_characters.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("template_version_id", "cast_key"),
    )

    with op.batch_alter_table("books") as batch:
        batch.add_column(
            sa.Column(
                "story_key",
                sa.String(length=64),
                nullable=False,
                server_default=DEFAULT_STORY_KEY,
            )
        )
        batch.add_column(sa.Column("template_version_id", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("cover_review", sa.JSON(), nullable=True))
        batch.create_foreign_key(
            "fk_books_template_version_id",
            "template_versions",
            ["template_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_books_template_version_id", ["template_version_id"], unique=False
        )

    with op.batch_alter_table("book_pages") as batch:
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("review", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("chosen_attempt", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("best_effort", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("image_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("book_pages") as batch:
        batch.drop_column("image_sha256")
        batch.drop_column("best_effort")
        batch.drop_column("chosen_attempt")
        batch.drop_column("review")
        batch.drop_column("attempt_count")

    with op.batch_alter_table("books") as batch:
        batch.drop_index("ix_books_template_version_id")
        batch.drop_constraint("fk_books_template_version_id", type_="foreignkey")
        batch.drop_column("cover_review")
        batch.drop_column("template_version_id")
        batch.drop_column("story_key")

    op.drop_table("template_version_characters")
    op.drop_index("ix_template_version_plates_plate_id", table_name="template_version_plates")
    op.drop_table("template_version_plates")
    op.drop_table("template_versions")
    op.drop_index("ix_template_plates_story_key", table_name="template_plates")
    op.drop_table("template_plates")
    op.drop_table("story_characters")
    op.drop_table("story_pages")
    op.drop_table("stories")
