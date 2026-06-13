"""holdings portfolio (A/B) tag + portfolio_config de-lever floor leverage

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-13

Adds the cohesive A+B portfolio bucket tag to positions, and a distinct
de-lever-floor leverage field (the level the -35/-45/-20 rule cuts TOWARD in a
drawdown) so it is never confused with the running target or the glide-path floor.

holdings:
  + portfolio  portfolio_bucket  NOT NULL default 'A'   (A = mechanical core, B = pilot)

portfolio_config:
  + delever_floor_leverage  Numeric(12,8)  NOT NULL default 1.06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

RATIO = sa.Numeric(12, 8)

# Native PostgreSQL enum type for the cohesive A+B portfolio bucket.
portfolio_bucket = sa.Enum("A", "B", name="portfolio_bucket")


def upgrade() -> None:
    bind = op.get_bind()

    # --- new enum type ----------------------------------------------------
    portfolio_bucket.create(bind, checkfirst=True)

    # --- holdings: portfolio bucket tag -----------------------------------
    op.add_column(
        "holdings",
        sa.Column(
            "portfolio",
            portfolio_bucket,
            nullable=False,
            server_default="A",
        ),
    )
    op.create_index("ix_holdings_portfolio", "holdings", ["portfolio"])

    # --- portfolio_config: de-lever floor leverage ------------------------
    op.add_column(
        "portfolio_config",
        sa.Column(
            "delever_floor_leverage", RATIO, nullable=False, server_default="1.06"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_column("portfolio_config", "delever_floor_leverage")
    op.drop_index("ix_holdings_portfolio", table_name="holdings")
    op.drop_column("holdings", "portfolio")
    portfolio_bucket.drop(bind, checkfirst=True)
