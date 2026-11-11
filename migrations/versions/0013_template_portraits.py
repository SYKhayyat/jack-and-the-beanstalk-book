"""Fixed portraits of supporting characters, pinned by template versions

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-10

A supporting character with no photo from the visitor used to reach a book with
no face at all: only the star's portrait and the visitors' own uploads ever
went to the drawer. `template_portraits` holds one approved portrait per
supporting character, drawn once for the story; `template_version_characters`
gains a nullable `portrait_id` so a version pins which portrait it uses, the way
it pins plates. Books reach it through their version, so a book made before this
keeps its old composition.

Nullability matches `app/db.py` column for column; the migration test builds
this schema and compares it to the models.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template_portraits",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("cast_key", sa.String(length=64), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["story_key"], ["stories.key"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_template_portraits_story_key", "template_portraits", ["story_key"], unique=False
    )
    with op.batch_alter_table("template_version_characters") as batch:
        batch.add_column(sa.Column("portrait_id", sa.String(length=32), nullable=True))
        batch.create_foreign_key(
            "fk_template_version_characters_portrait_id",
            "template_portraits",
            ["portrait_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_template_version_characters_portrait_id",
        "template_version_characters",
        ["portrait_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_template_version_characters_portrait_id", table_name="template_version_characters"
    )
    with op.batch_alter_table("template_version_characters") as batch:
        batch.drop_constraint("fk_template_version_characters_portrait_id", type_="foreignkey")
        batch.drop_column("portrait_id")
    op.drop_index("ix_template_portraits_story_key", table_name="template_portraits")
    op.drop_table("template_portraits")
