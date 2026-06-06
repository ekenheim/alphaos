"""SQLAlchemy ORM models for AlphaOS — V2-FRONTIER portfolio tracker.

The app tracks a leveraged, multi-sleeve allocation (Avanza ISK, SEK base):

  portfolio_config -- singleton: leverage targets, de-lever thresholds, rates, etc.
  sleeves          -- allocation buckets (CNDX/VVSM/RAW/CA/LOWVOL) + target weights
  holdings         -- the actual instruments held, each under a sleeve (market value, SEK)
  nav_snapshots    -- the NAV-index/TWR ledger: equity, contributions, loan, drawdown,
                      leverage, belaningsgrad, de-lever status per period

Money/quantity columns use Numeric for exact precision (never float). All monetary
values are in the portfolio base currency (SEK) unless noted.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Money in base currency (SEK). Weights/ratios use a wider fractional scale.
MONEY = Numeric(20, 4)
RATIO = Numeric(12, 8)
QTY = Numeric(24, 8)
PRICE = Numeric(20, 8)  # per-unit price in the instrument's own currency


class SleeveKind(str, enum.Enum):
    beta_core = "beta_core"
    tilt = "tilt"
    discretionary_equity = "discretionary_equity"
    cross_asset_insurance = "cross_asset_insurance"
    low_vol_carve = "low_vol_carve"
    other = "other"


class AssetClass(str, enum.Enum):
    equity = "equity"
    etf = "etf"
    bond = "bond"
    commodity = "commodity"
    cash = "cash"
    other = "other"


class DeleverStatus(str, enum.Enum):
    normal = "normal"          # DD above the -35% trigger
    half = "half"              # DD <= -35%: repay half the loan
    full = "full"              # DD <= -45%: repay the whole loan
    reentry = "reentry"        # recovering, re-levering in halves


class PriceSource(str, enum.Enum):
    minio = "minio"            # latest close from MinIO stocks-us
    manual = "manual"          # current price typed by the operator
    cost = "cost"              # no current price -> valued at purchase price
    none = "none"             # not priced


class TransactionKind(str, enum.Enum):
    buy = "buy"
    sell = "sell"


class TxnSource(str, enum.Enum):
    avanza = "avanza"          # imported from an Avanza transaktioner CSV
    manual = "manual"          # entered in-app (e.g. a rebalance leg)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PortfolioConfig(Base):
    """Singleton (id=1) holding the V2-FRONTIER risk/leverage parameters."""

    __tablename__ = "portfolio_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    base_currency: Mapped[str] = mapped_column(String(8), default="SEK")
    account_label: Mapped[str] = mapped_column(String(64), default="Avanza ISK")

    # Leverage + glide path (effective leverage by portfolio size).
    leverage_target: Mapped[float] = mapped_column(RATIO, default=1.30)
    leverage_floor: Mapped[float] = mapped_column(RATIO, default=1.00)
    glide_low_assets: Mapped[float] = mapped_column(MONEY, default=2_500_000)
    glide_high_assets: Mapped[float] = mapped_column(MONEY, default=10_000_000)

    # Financing.
    blended_rate: Mapped[float] = mapped_column(RATIO, default=0.0234)
    repriced_rate: Mapped[float] = mapped_column(RATIO, default=0.0359)
    belaningsgrad_cliff: Mapped[float] = mapped_column(RATIO, default=0.25)

    # De-lever rule (binding) + ruin boundary, as NAV-index drawdown fractions.
    delever_half_dd: Mapped[float] = mapped_column(RATIO, default=-0.35)
    delever_full_dd: Mapped[float] = mapped_column(RATIO, default=-0.45)
    reentry_recovery: Mapped[float] = mapped_column(RATIO, default=0.20)
    forced_sale_dd: Mapped[float] = mapped_column(RATIO, default=-0.57)

    external_reserve: Mapped[float] = mapped_column(MONEY, default=75_000)
    planning_cagr_low: Mapped[float] = mapped_column(RATIO, default=0.10)
    planning_cagr_high: Mapped[float] = mapped_column(RATIO, default=0.16)

    # FX rates to SEK (cached from Riksbank/ECB; editable). 1 unit -> SEK.
    fx_usd_sek: Mapped[float] = mapped_column(RATIO, default=9.34)
    fx_eur_sek: Mapped[float] = mapped_column(RATIO, default=10.87)
    fx_as_of: Mapped[dt.date | None] = mapped_column(Date, default=None)
    fx_source: Mapped[str | None] = mapped_column(String(32), default=None)

    notes: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )


class Sleeve(Base):
    __tablename__ = "sleeves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[SleeveKind] = mapped_column(
        Enum(SleeveKind, name="sleeve_kind"), default=SleeveKind.other
    )
    target_weight: Mapped[float] = mapped_column(RATIO, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )

    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="sleeve", cascade="all, delete-orphan", order_by="Holding.symbol"
    )


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sleeve_id: Mapped[int | None] = mapped_column(
        ForeignKey("sleeves.id", ondelete="SET NULL"), index=True, default=None
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(16), default=None)
    name: Mapped[str | None] = mapped_column(String(160), default=None)
    asset_class: Mapped[AssetClass] = mapped_column(
        Enum(AssetClass, name="asset_class"), default=AssetClass.equity
    )
    currency: Mapped[str] = mapped_column(String(8), default="SEK")
    quantity: Mapped[float] = mapped_column(QTY, default=0)
    # Purchase price per unit, in the holding's own currency (cost basis driver).
    avg_price: Mapped[float] = mapped_column(PRICE, default=0)
    # Exact SEK cost paid (from the Avanza CSV Belopp); None when not known.
    cost_basis_sek: Mapped[float | None] = mapped_column(MONEY, default=None)
    # Date the position was first opened (earliest buy); for holding-period return.
    acquired_at: Mapped[dt.date | None] = mapped_column(Date, default=None)
    # Latest price per unit (instrument currency): MinIO close or a manual entry.
    last_price: Mapped[float | None] = mapped_column(PRICE, default=None)
    last_price_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    price_source: Mapped[PriceSource] = mapped_column(
        Enum(PriceSource, name="price_source"), default=PriceSource.none
    )
    as_of: Mapped[dt.date | None] = mapped_column(Date, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )

    sleeve: Mapped["Sleeve | None"] = relationship(back_populates="holdings")


class Transaction(Base):
    """The ledger: one row per buy/sell. Holdings (qty/avg/cost/acquired) are
    DERIVED by aggregating these (average-cost). Source 'avanza' rows come from a
    CSV import (replaced by date range on re-import); 'manual' rows are entered
    in-app (e.g. a rebalance leg)."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    isin: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), default=None)
    name: Mapped[str | None] = mapped_column(String(160), default=None)
    currency: Mapped[str] = mapped_column(String(8), default="SEK")
    kind: Mapped[TransactionKind] = mapped_column(Enum(TransactionKind, name="transaction_kind"))
    quantity: Mapped[float] = mapped_column(QTY)            # positive magnitude
    price: Mapped[float] = mapped_column(PRICE)             # instrument ccy per unit
    amount_sek: Mapped[float | None] = mapped_column(MONEY, default=None)  # CSV Belopp (signed)
    fees_sek: Mapped[float] = mapped_column(MONEY, default=0)
    fx_rate: Mapped[float | None] = mapped_column(RATIO, default=None)     # CSV Valutakurs
    source: Mapped[TxnSource] = mapped_column(
        Enum(TxnSource, name="txn_source"), default=TxnSource.manual, index=True
    )
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NavSnapshot(Base):
    """One row per NAV-ledger observation. TWR/NAV-index/drawdown are computed
    (see alphaos.db.nav) from equity ex-contributions, never raw account value."""

    __tablename__ = "nav_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)

    gross_asset_value: Mapped[float] = mapped_column(MONEY)          # total holdings MV
    loan_balance: Mapped[float] = mapped_column(MONEY, default=0)    # värdepapperskredit
    net_contribution: Mapped[float] = mapped_column(MONEY, default=0)  # cash in (+) / out (-) this period
    equity: Mapped[float] = mapped_column(MONEY)                     # gross - loan

    twr_period: Mapped[float | None] = mapped_column(RATIO, default=None)
    nav_index: Mapped[float] = mapped_column(Numeric(20, 10))
    peak_nav_index: Mapped[float] = mapped_column(Numeric(20, 10))
    drawdown: Mapped[float] = mapped_column(RATIO, default=0)

    effective_leverage: Mapped[float | None] = mapped_column(RATIO, default=None)  # gross/equity
    belaningsgrad: Mapped[float | None] = mapped_column(RATIO, default=None)       # loan/gross
    delever_status: Mapped[DeleverStatus] = mapped_column(
        Enum(DeleverStatus, name="delever_status"), default=DeleverStatus.normal
    )

    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
