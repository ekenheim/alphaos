"""initial schema: strategies, backtests, positions, trade_events

Revision ID: 0001
Revises:
Create Date: 2026-06-06

Hand-authored initial migration. Creates the four AlphaOS tables exactly as
defined in alphaos.db.models, including the four native PostgreSQL enum types,
foreign keys (with ondelete behavior), and indexes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Money/quantity precision (matches models.NUM).
NUM = sa.Numeric(20, 8)

# Native PostgreSQL enum types. Each type is used by exactly one table, so the
# CREATE TYPE is emitted (once) as part of that table's create_table below; the
# types are dropped explicitly in downgrade().
strategy_status = sa.Enum(
    "experimental", "active", "archived", name="strategy_status"
)
position_side = sa.Enum("long", "short", name="position_side")
position_status = sa.Enum("open", "closed", name="position_status")
trade_action = sa.Enum(
    "open", "add", "trim", "close", "rebalance", name="trade_action"
)


def upgrade() -> None:
    # --- strategies -------------------------------------------------------
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", strategy_status, nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
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
    op.create_index("ix_strategies_slug", "strategies", ["slug"], unique=True)
    op.create_index("ix_strategies_status", "strategies", ["status"], unique=False)

    # --- backtests --------------------------------------------------------
    op.create_table(
        "backtests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("n_trades", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("avg_r", sa.Numeric(12, 6), nullable=True),
        sa.Column("sharpe", sa.Numeric(12, 6), nullable=True),
        sa.Column("max_dd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cagr", sa.Numeric(12, 6), nullable=True),
        sa.Column("total_r", sa.Numeric(12, 6), nullable=True),
        sa.Column("placebo_pass", sa.Boolean(), nullable=True),
        sa.Column("equity_curve", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"], ["strategies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_backtests_strategy_id", "backtests", ["strategy_id"], unique=False
    )
    op.create_index("ix_backtests_symbol", "backtests", ["symbol"], unique=False)
    op.create_index(
        "ix_backtests_created_at", "backtests", ["created_at"], unique=False
    )

    # --- positions --------------------------------------------------------
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", position_side, nullable=False),
        sa.Column("status", position_status, nullable=False),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("avg_entry_px", NUM, nullable=False),
        sa.Column("realized_pnl", NUM, nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=True),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["strategy_id"], ["strategies.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_symbol", "positions", ["symbol"], unique=False)
    op.create_index("ix_positions_status", "positions", ["status"], unique=False)
    op.create_index(
        "ix_positions_strategy_id", "positions", ["strategy_id"], unique=False
    )

    # --- trade_events -----------------------------------------------------
    op.create_table(
        "trade_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=True),
        sa.Column("action", trade_action, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("price", NUM, nullable=False),
        sa.Column("fees", NUM, nullable=False),
        sa.Column("realized_pnl", NUM, nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"], ["strategies.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_trade_events_position_id", "trade_events", ["position_id"], unique=False
    )
    op.create_index(
        "ix_trade_events_symbol", "trade_events", ["symbol"], unique=False
    )
    op.create_index(
        "ix_trade_events_batch_id", "trade_events", ["batch_id"], unique=False
    )
    op.create_index("ix_trade_events_ts", "trade_events", ["ts"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_trade_events_ts", table_name="trade_events")
    op.drop_index("ix_trade_events_batch_id", table_name="trade_events")
    op.drop_index("ix_trade_events_symbol", table_name="trade_events")
    op.drop_index("ix_trade_events_position_id", table_name="trade_events")
    op.drop_table("trade_events")

    op.drop_index("ix_positions_strategy_id", table_name="positions")
    op.drop_index("ix_positions_status", table_name="positions")
    op.drop_index("ix_positions_symbol", table_name="positions")
    op.drop_table("positions")

    op.drop_index("ix_backtests_created_at", table_name="backtests")
    op.drop_index("ix_backtests_symbol", table_name="backtests")
    op.drop_index("ix_backtests_strategy_id", table_name="backtests")
    op.drop_table("backtests")

    op.drop_index("ix_strategies_status", table_name="strategies")
    op.drop_index("ix_strategies_slug", table_name="strategies")
    op.drop_table("strategies")

    # --- enum types -------------------------------------------------------
    trade_action.drop(bind, checkfirst=True)
    position_status.drop(bind, checkfirst=True)
    position_side.drop(bind, checkfirst=True)
    strategy_status.drop(bind, checkfirst=True)
