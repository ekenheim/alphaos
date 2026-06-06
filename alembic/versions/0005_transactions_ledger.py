"""add transactions ledger (holdings become derived)

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(20, 4)
RATIO = sa.Numeric(12, 8)
QTY = sa.Numeric(24, 8)
PRICE = sa.Numeric(20, 8)

transaction_kind = postgresql.ENUM("buy", "sell", name="transaction_kind", create_type=False)
txn_source = postgresql.ENUM("avanza", "manual", name="txn_source", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    transaction_kind.create(bind, checkfirst=True)
    txn_source.create(bind, checkfirst=True)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("isin", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("name", sa.String(160), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False, server_default="SEK"),
        sa.Column("kind", transaction_kind, nullable=False),
        sa.Column("quantity", QTY, nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("amount_sek", MONEY, nullable=True),
        sa.Column("fees_sek", MONEY, nullable=False, server_default="0"),
        sa.Column("fx_rate", RATIO, nullable=True),
        sa.Column("source", txn_source, nullable=False, server_default="manual"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_transactions_date", "transactions", ["date"])
    op.create_index("ix_transactions_isin", "transactions", ["isin"])
    op.create_index("ix_transactions_source", "transactions", ["source"])


def downgrade() -> None:
    op.drop_index("ix_transactions_source", table_name="transactions")
    op.drop_index("ix_transactions_isin", table_name="transactions")
    op.drop_index("ix_transactions_date", table_name="transactions")
    op.drop_table("transactions")
    bind = op.get_bind()
    txn_source.drop(bind, checkfirst=True)
    transaction_kind.drop(bind, checkfirst=True)
