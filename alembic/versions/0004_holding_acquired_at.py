"""add holdings.acquired_at (first-purchase date)

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("holdings", sa.Column("acquired_at", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("holdings", "acquired_at")
