"""wallet_transactions, wallet_refunds

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Accounts and money: the ledger, and the refund requests drawn on it.

    The balance is never stored, only summed from `wallet_transactions`, so the
    transaction-history page and the balance cannot disagree. The unique
    `(user_id, kind, reference)` triple is the idempotency guarantee: a
    re-delivered Stripe session or a retried book charge cannot be applied
    twice. `provider_refund_ids` is JSON because one request may have to refund
    more than one funding payment.

    Nullability matches `app/db.py` column for column; the migration test
    builds this schema and compares it to the models.
    """
    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=True),
        sa.Column("payment_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "kind", "reference", name="uq_wallet_transaction_reference"
        ),
    )
    op.create_index(
        "ix_wallet_transactions_user_id", "wallet_transactions", ["user_id"], unique=False
    )
    op.create_table(
        "wallet_refunds",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_refund_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wallet_refunds_user_id", "wallet_refunds", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_wallet_refunds_user_id", table_name="wallet_refunds")
    op.drop_table("wallet_refunds")
    op.drop_index("ix_wallet_transactions_user_id", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
