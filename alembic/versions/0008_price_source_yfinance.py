"""add 'yfinance' to the price_source enum (Yahoo price fallback)

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block; use an
    # autocommit block so it commits on its own.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE price_source ADD VALUE IF NOT EXISTS 'yfinance'")


def downgrade() -> None:
    # Postgres has no DROP VALUE; removing an enum value means recreating the type.
    # Left as a no-op — an unused enum value is harmless.
    pass
