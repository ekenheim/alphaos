"""holdings cost-basis/pricing + portfolio_config FX columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-06

Extends the V2-FRONTIER schema so holdings carry their own cost basis and last
known price (instead of a stored market_value), and portfolio_config caches the
USD/EUR -> SEK FX rates used to value non-SEK holdings.

holdings:
  + avg_price        Numeric(20,8) NOT NULL default 0   (purchase price / unit)
  + cost_basis_sek   Numeric(20,4) NULL                 (exact SEK cost paid)
  + last_price       Numeric(20,8) NULL                 (latest price / unit)
  + last_price_date  Date          NULL
  + price_source     price_source  NOT NULL default 'none'
  - market_value (dropped; value is now computed qty * price * FX)

portfolio_config:
  + fx_usd_sek  Numeric(12,8) NOT NULL default 9.34
  + fx_eur_sek  Numeric(12,8) NOT NULL default 10.87
  + fx_as_of    Date          NULL
  + fx_source   String(32)    NULL
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Column precision (matches alphaos.db.models).
MONEY = sa.Numeric(20, 4)
RATIO = sa.Numeric(12, 8)
PRICE = sa.Numeric(20, 8)

# Native PostgreSQL enum type for the holdings pricing source.
price_source = sa.Enum(
    "minio", "manual", "cost", "none", name="price_source"
)


def upgrade() -> None:
    bind = op.get_bind()

    # --- new enum type ----------------------------------------------------
    price_source.create(bind, checkfirst=True)

    # --- holdings: cost basis + pricing -----------------------------------
    op.add_column(
        "holdings",
        sa.Column(
            "avg_price", PRICE, nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "holdings",
        sa.Column("cost_basis_sek", MONEY, nullable=True),
    )
    op.add_column(
        "holdings",
        sa.Column("last_price", PRICE, nullable=True),
    )
    op.add_column(
        "holdings",
        sa.Column("last_price_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "holdings",
        sa.Column(
            "price_source",
            price_source,
            nullable=False,
            server_default="none",
        ),
    )
    op.drop_column("holdings", "market_value")

    # --- portfolio_config: cached FX rates --------------------------------
    op.add_column(
        "portfolio_config",
        sa.Column(
            "fx_usd_sek", RATIO, nullable=False, server_default="9.34"
        ),
    )
    op.add_column(
        "portfolio_config",
        sa.Column(
            "fx_eur_sek", RATIO, nullable=False, server_default="10.87"
        ),
    )
    op.add_column(
        "portfolio_config",
        sa.Column("fx_as_of", sa.Date(), nullable=True),
    )
    op.add_column(
        "portfolio_config",
        sa.Column("fx_source", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()

    # --- portfolio_config: drop cached FX rates ---------------------------
    op.drop_column("portfolio_config", "fx_source")
    op.drop_column("portfolio_config", "fx_as_of")
    op.drop_column("portfolio_config", "fx_eur_sek")
    op.drop_column("portfolio_config", "fx_usd_sek")

    # --- holdings: restore market_value, drop cost basis + pricing --------
    op.add_column(
        "holdings",
        sa.Column(
            "market_value", MONEY, nullable=False, server_default="0"
        ),
    )
    op.drop_column("holdings", "price_source")
    op.drop_column("holdings", "last_price_date")
    op.drop_column("holdings", "last_price")
    op.drop_column("holdings", "cost_basis_sek")
    op.drop_column("holdings", "avg_price")

    # --- drop the new enum type -------------------------------------------
    price_source.drop(bind, checkfirst=True)
