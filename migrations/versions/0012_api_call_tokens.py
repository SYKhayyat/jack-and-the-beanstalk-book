"""Token counts on the cost ledger

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-10

OpenAI bills an image edit by token, and most of the bill is the reference
images sent in, not the picture that comes out. Each `api_calls` row now keeps
the counts the response reported (`text_input_tokens`, `image_input_tokens`,
`output_tokens`) and is priced from them.

Rows written before this have no counts and keep the price they were logged at
(the output-only per-image figure, which undercounted).

Nullability matches `app/db.py` column for column; the migration test builds
this schema and compares it to the models.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None

_COLUMNS = ("text_input_tokens", "image_input_tokens", "output_tokens")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("api_calls", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("api_calls") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
