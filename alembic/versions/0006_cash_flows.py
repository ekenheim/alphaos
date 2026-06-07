"""add cash_flows ledger (deposits/withdrawals; drives net_contribution)

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(20, 4)

cash_flow_kind = postgresql.ENUM(
    "deposit", "withdrawal", name="cash_flow_kind", create_type=False
)
# Reuse the txn_source enum created in 0005 — do NOT recreate it.
txn_source = postgresql.ENUM("avanza", "manual", name="txn_source", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    cash_flow_kind.create(bind, checkfirst=True)

    op.create_table(
        "cash_flows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount_sek", MONEY, nullable=False),
        sa.Column("kind", cash_flow_kind, nullable=False),
        sa.Column("source", txn_source, nullable=False, server_default="manual"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_cash_flows_date", "cash_flows", ["date"])
    op.create_index("ix_cash_flows_source", "cash_flows", ["source"])


def downgrade() -> None:
    op.drop_index("ix_cash_flows_source", table_name="cash_flows")
    op.drop_index("ix_cash_flows_date", table_name="cash_flows")
    op.drop_table("cash_flows")
    bind = op.get_bind()
    cash_flow_kind.drop(bind, checkfirst=True)
    # txn_source enum is owned by 0005 — not dropped here.
