"""SQLAlchemy ORM models for the AlphaOS ledger + strategy archive.

  strategies     -- catalog of strategies tried (seeded from alphaos.setups)
  backtests      -- backtest runs per strategy + their performance metrics
  positions      -- current ownership (mutable: qty, avg cost, status)
  trade_events   -- append-only executions that mutate positions (open/add/trim/close/rebalance)

Money/quantity columns use Numeric(20, 8) so fractional/crypto sizes and prices
keep exact precision (never float).
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
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


# Numeric precision for prices, quantities, and PnL.
NUM = Numeric(20, 8)


class StrategyStatus(str, enum.Enum):
    experimental = "experimental"
    active = "active"
    archived = "archived"


class PositionStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


class Side(str, enum.Enum):
    long = "long"
    short = "short"


class Action(str, enum.Enum):
    open = "open"
    add = "add"
    trim = "trim"
    close = "close"
    rebalance = "rebalance"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[StrategyStatus] = mapped_column(
        Enum(StrategyStatus, name="strategy_status"),
        default=StrategyStatus.experimental,
        index=True,
    )
    params: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )

    backtests: Mapped[list["Backtest"]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan"
    )
    positions: Mapped[list["Position"]] = relationship(back_populates="strategy")


class Backtest(Base):
    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    interval: Mapped[str] = mapped_column(String(8))
    start_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    end_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    params: Mapped[dict | None] = mapped_column(JSON, default=None)

    # Performance metrics (mirror alphaos.backtest.BacktestResult).
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    avg_r: Mapped[float | None] = mapped_column(Numeric(12, 6), default=None)
    sharpe: Mapped[float | None] = mapped_column(Numeric(12, 6), default=None)
    max_dd: Mapped[float | None] = mapped_column(Numeric(12, 6), default=None)
    cagr: Mapped[float | None] = mapped_column(Numeric(12, 6), default=None)
    total_r: Mapped[float | None] = mapped_column(Numeric(12, 6), default=None)
    placebo_pass: Mapped[bool | None] = mapped_column(Boolean, default=None)

    equity_curve: Mapped[dict | None] = mapped_column(JSON, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    strategy: Mapped["Strategy"] = relationship(back_populates="backtests")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[Side] = mapped_column(
        Enum(Side, name="position_side"), default=Side.long
    )
    status: Mapped[PositionStatus] = mapped_column(
        Enum(PositionStatus, name="position_status"),
        default=PositionStatus.open,
        index=True,
    )
    qty: Mapped[float] = mapped_column(NUM, default=0)
    avg_entry_px: Mapped[float] = mapped_column(NUM, default=0)
    realized_pnl: Mapped[float] = mapped_column(NUM, default=0)

    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), default=None, index=True
    )
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    strategy: Mapped["Strategy | None"] = relationship(back_populates="positions")
    events: Mapped[list["TradeEvent"]] = relationship(
        back_populates="position",
        cascade="all, delete-orphan",
        order_by="TradeEvent.ts",
    )


class TradeEvent(Base):
    __tablename__ = "trade_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"), index=True
    )
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), default=None
    )
    action: Mapped[Action] = mapped_column(Enum(Action, name="trade_action"))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[float] = mapped_column(NUM)
    price: Mapped[float] = mapped_column(NUM)
    fees: Mapped[float] = mapped_column(NUM, default=0)
    realized_pnl: Mapped[float] = mapped_column(NUM, default=0)
    # Groups the executions of a single rebalance together.
    batch_id: Mapped[str | None] = mapped_column(String(36), default=None, index=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    position: Mapped["Position"] = relationship(back_populates="events")
