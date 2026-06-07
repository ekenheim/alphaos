"""add cost_basis to nav_snapshots (enables money-terms P&L / value-vs-cost over time)

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(20, 4)


def upgrade() -> None:
    op.add_column("nav_snapshots", sa.Column("cost_basis", MONEY, nullable=True))


def downgrade() -> None:
    op.drop_column("nav_snapshots", "cost_basis")
