"""Review every picture; keep the studio's history

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09

* `books.review_required`: every picture of every new book is reviewed, plated
  or not, and a book cannot complete without a verdict on each. Books made
  before this keep `false` — nothing looked at their pictures and nothing ever
  will, so asking would only stop them from being edited. Books that were
  already pinned to a template version were already reviewed and keep `true`.
* `book_characters.generation_id` / `settings`: which portrait draft a
  photographed cast member came from, and the look it was drawn with.
* `portrait_attempts`: every portrait shown in the studio and every take
  picked, with its settings, the chips ticked before it, and the backend.

Nullability matches `app/db.py` column for column; the migration test builds
this schema and compares it to the models.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "books",
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute(sa.text("UPDATE books SET review_required = (template_version_id IS NOT NULL)"))
    op.add_column(
        "book_characters", sa.Column("generation_id", sa.String(length=64), nullable=True)
    )
    op.add_column("book_characters", sa.Column("settings", sa.JSON(), nullable=True))
    op.create_table(
        "portrait_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("story_key", sa.String(length=64), nullable=False),
        sa.Column("cast_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("rejection", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("takes", sa.Integer(), nullable=False),
        sa.Column("chosen_index", sa.Integer(), nullable=True),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_portrait_attempts_user_id", "portrait_attempts", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_portrait_attempts_user_id", table_name="portrait_attempts")
    op.drop_table("portrait_attempts")
    with op.batch_alter_table("book_characters") as batch:
        batch.drop_column("settings")
        batch.drop_column("generation_id")
    with op.batch_alter_table("books") as batch:
        batch.drop_column("review_required")
