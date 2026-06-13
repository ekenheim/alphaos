"""FX history + sleeve-weight history tables; drop external_reserve + planning_cagr

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-13

portfolio_config:
  - external_reserve     (dropped; removed from the dashboard)
  - planning_cagr_low    (dropped; replaced by computed CAGR-since-inception)
  - planning_cagr_high   (dropped)

new fx_rates:
  append-only daily FX history (one row per rate date).
new sleeve_weight_history:
  dated log of sleeve target-weight create/update/delete events.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(20, 4)
RATIO = sa.Numeric(12, 8)


def upgrade() -> None:
    # --- portfolio_config: drop retired fields ----------------------------
    op.drop_column("portfolio_config", "external_reserve")
    op.drop_column("portfolio_config", "planning_cagr_low")
    op.drop_column("portfolio_config", "planning_cagr_high")

    # --- fx_rates: daily FX history ---------------------------------------
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("usd_sek", RATIO, nullable=False),
        sa.Column("eur_sek", RATIO, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_fx_rates_as_of", "fx_rates", ["as_of"], unique=True)

    # --- sleeve_weight_history: dated allocation trail --------------------
    op.create_table(
        "sleeve_weight_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("sleeve_code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("target_weight", RATIO, nullable=False, server_default="0"),
        sa.Column("event", sa.String(length=16), nullable=False),
    )
    op.create_index("ix_sleeve_weight_history_changed_at", "sleeve_weight_history", ["changed_at"])
    op.create_index("ix_sleeve_weight_history_sleeve_code", "sleeve_weight_history", ["sleeve_code"])


def downgrade() -> None:
    op.drop_index("ix_sleeve_weight_history_sleeve_code", table_name="sleeve_weight_history")
    op.drop_index("ix_sleeve_weight_history_changed_at", table_name="sleeve_weight_history")
    op.drop_table("sleeve_weight_history")

    op.drop_index("ix_fx_rates_as_of", table_name="fx_rates")
    op.drop_table("fx_rates")

    op.add_column("portfolio_config", sa.Column("planning_cagr_high", RATIO, nullable=False, server_default="0.16"))
    op.add_column("portfolio_config", sa.Column("planning_cagr_low", RATIO, nullable=False, server_default="0.10"))
    op.add_column("portfolio_config", sa.Column("external_reserve", MONEY, nullable=False, server_default="75000"))
