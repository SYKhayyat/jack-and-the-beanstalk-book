"""Pre-made pictures for pages the star is not in, pinned by template versions

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-11

A page without the star shows only fixed characters, so its picture is the same
in every book whose visitor gave no photo of anyone on it. `template_premade_pages`
holds one approved picture per such page, made once from the page's plate;
`template_version_plates` gains a nullable `premade_id` so a version pins it the
way it pins the plate. A book drawn on that version uses the picture instead of
an image call. Books made before this keep their old composition.

A pre-made picture belongs to the plate it was painted onto (`plate_id`): a new
plate for the page leaves it out of later versions rather than pairing a
picture with a background it was not made from.

Nullability matches `app/db.py` column for column; the migration test builds
this schema and compares it to the models.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision: str | None = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template_premade_pages",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("plate_id", sa.String(length=32), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["story_key"], ["stories.key"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plate_id"], ["template_plates.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_template_premade_pages_story_key",
        "template_premade_pages",
        ["story_key"],
        unique=False,
    )
    op.create_index(
        "ix_template_premade_pages_plate_id",
        "template_premade_pages",
        ["plate_id"],
        unique=False,
    )
    with op.batch_alter_table("template_version_plates") as batch:
        batch.add_column(sa.Column("premade_id", sa.String(length=32), nullable=True))
        batch.create_foreign_key(
            "fk_template_version_plates_premade_id",
            "template_premade_pages",
            ["premade_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_template_version_plates_premade_id",
        "template_version_plates",
        ["premade_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_template_version_plates_premade_id", table_name="template_version_plates"
    )
    with op.batch_alter_table("template_version_plates") as batch:
        batch.drop_constraint("fk_template_version_plates_premade_id", type_="foreignkey")
        batch.drop_column("premade_id")
    op.drop_index("ix_template_premade_pages_plate_id", table_name="template_premade_pages")
    op.drop_index("ix_template_premade_pages_story_key", table_name="template_premade_pages")
    op.drop_table("template_premade_pages")
