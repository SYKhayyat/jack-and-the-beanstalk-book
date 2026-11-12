"""Remember the child once, so the next book skips the photo and the portrait

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-12

A portrait is drawn in its story's costume, from the visitor's photograph, and
both are thrown away with the draft a day later. That made every book start
from an upload. This table keeps the star's approved portrait, the photograph
it was drawn from, the look settings and the name, one row per account, so the
studio can hand the same portrait back as a fresh draft: no upload, no image
call, nothing charged.

The portrait and the photo are blobs like a book's, so they follow the same
storage rule (inline, or R2 when it is configured). `portrait_sha256` versions
the studio's URL, and `story_key` records which tale's costume the portrait
wears — it can only be reused by that story.

One row per person: remembering another child replaces it, and forgetting is a
delete. New table, so every existing database migrates without a rewrite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "child_profiles",
        sa.Column(
            "user_id",
            sa.String(length=128),
            sa.ForeignKey("users.sub", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("name", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("photo_gender", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("portrait", sa.LargeBinary(), nullable=False),
        sa.Column("portrait_media_type", sa.String(length=50), nullable=False),
        sa.Column("portrait_sha256", sa.String(length=64), nullable=False),
        sa.Column("photo", sa.LargeBinary(), nullable=False),
        sa.Column("photo_media_type", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("child_profiles")
