"""v2-frontier schema: portfolio_config, sleeves, holdings, nav_snapshots

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-06

Replaces the legacy strategies/backtests/positions/trade_events schema with the
V2-FRONTIER portfolio tracker tables defined in alphaos.db.models. Drops the old
tables and their native PostgreSQL enum types, then creates the four new tables
(with native enums sleeve_kind / asset_class / delever_status, FKs, and indexes).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Column precision (matches alphaos.db.models).
MONEY = sa.Numeric(20, 4)
RATIO = sa.Numeric(12, 8)
QTY = sa.Numeric(24, 8)
NAVIX = sa.Numeric(20, 10)

# Native PostgreSQL enum types used by the new tables.
sleeve_kind = sa.Enum(
    "beta_core",
    "tilt",
    "discretionary_equity",
    "cross_asset_insurance",
    "low_vol_carve",
    "other",
    name="sleeve_kind",
)
asset_class = sa.Enum(
    "equity", "etf", "bond", "commodity", "cash", "other", name="asset_class"
)
delever_status = sa.Enum(
    "normal", "half", "full", "reentry", name="delever_status"
)


def upgrade() -> None:
    # --- drop the legacy schema ------------------------------------------
    # Tables first (FK-aware order), then the orphaned enum types. Guarded with
    # IF EXISTS / CASCADE so a partially-applied or absent old schema is fine.
    op.execute("DROP TABLE IF EXISTS trade_events CASCADE")
    op.execute("DROP TABLE IF EXISTS positions CASCADE")
    op.execute("DROP TABLE IF EXISTS backtests CASCADE")
    op.execute("DROP TABLE IF EXISTS strategies CASCADE")

    op.execute("DROP TYPE IF EXISTS trade_action")
    op.execute("DROP TYPE IF EXISTS position_status")
    op.execute("DROP TYPE IF EXISTS position_side")
    op.execute("DROP TYPE IF EXISTS strategy_status")

    # --- portfolio_config (singleton) ------------------------------------
    op.create_table(
        "portfolio_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("base_currency", sa.String(length=8), nullable=False),
        sa.Column("account_label", sa.String(length=64), nullable=False),
        sa.Column("leverage_target", RATIO, nullable=False),
        sa.Column("leverage_floor", RATIO, nullable=False),
        sa.Column("glide_low_assets", MONEY, nullable=False),
        sa.Column("glide_high_assets", MONEY, nullable=False),
        sa.Column("blended_rate", RATIO, nullable=False),
        sa.Column("repriced_rate", RATIO, nullable=False),
        sa.Column("belaningsgrad_cliff", RATIO, nullable=False),
        sa.Column("delever_half_dd", RATIO, nullable=False),
        sa.Column("delever_full_dd", RATIO, nullable=False),
        sa.Column("reentry_recovery", RATIO, nullable=False),
        sa.Column("forced_sale_dd", RATIO, nullable=False),
        sa.Column("external_reserve", MONEY, nullable=False),
        sa.Column("planning_cagr_low", RATIO, nullable=False),
        sa.Column("planning_cagr_high", RATIO, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- sleeves ----------------------------------------------------------
    op.create_table(
        "sleeves",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sleeve_kind, nullable=False),
        sa.Column("target_weight", RATIO, nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sleeves_code", "sleeves", ["code"], unique=True)

    # --- holdings ---------------------------------------------------------
    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sleeve_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("isin", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=True),
        sa.Column("asset_class", asset_class, nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("quantity", QTY, nullable=False),
        sa.Column("market_value", MONEY, nullable=False),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["sleeve_id"], ["sleeves.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_holdings_sleeve_id", "holdings", ["sleeve_id"], unique=False)
    op.create_index("ix_holdings_symbol", "holdings", ["symbol"], unique=False)

    # --- nav_snapshots ----------------------------------------------------
    op.create_table(
        "nav_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("gross_asset_value", MONEY, nullable=False),
        sa.Column("loan_balance", MONEY, nullable=False),
        sa.Column("net_contribution", MONEY, nullable=False),
        sa.Column("equity", MONEY, nullable=False),
        sa.Column("twr_period", RATIO, nullable=True),
        sa.Column("nav_index", NAVIX, nullable=False),
        sa.Column("peak_nav_index", NAVIX, nullable=False),
        sa.Column("drawdown", RATIO, nullable=False),
        sa.Column("effective_leverage", RATIO, nullable=True),
        sa.Column("belaningsgrad", RATIO, nullable=True),
        sa.Column("delever_status", delever_status, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_nav_snapshots_as_of", "nav_snapshots", ["as_of"], unique=True
    )


def downgrade() -> None:
    bind = op.get_bind()

    # --- drop the new tables (FK-aware order) + indexes ------------------
    op.drop_index("ix_nav_snapshots_as_of", table_name="nav_snapshots")
    op.drop_table("nav_snapshots")

    op.drop_index("ix_holdings_symbol", table_name="holdings")
    op.drop_index("ix_holdings_sleeve_id", table_name="holdings")
    op.drop_table("holdings")

    op.drop_index("ix_sleeves_code", table_name="sleeves")
    op.drop_table("sleeves")

    op.drop_table("portfolio_config")

    # --- drop the new enum types -----------------------------------------
    delever_status.drop(bind, checkfirst=True)
    asset_class.drop(bind, checkfirst=True)
    sleeve_kind.drop(bind, checkfirst=True)

    # Best-effort: the legacy 0001 schema is intentionally NOT recreated here.
    # Re-running `alembic upgrade 0001` from this point is unsupported; downgrade
    # leaves the database with the old tables absent.
