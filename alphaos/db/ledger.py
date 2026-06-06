"""Live position ledger — services that record executions and keep positions in sync.

A position is mutable current ownership; trade_events is the append-only log of
executions that change it. Recording an execution updates the position's quantity,
weighted-average entry, and realized PnL atomically.

Action semantics:
  open / add   -> increase exposure (creates the position if none is open)
  trim         -> reduce exposure by `qty` (partial)
  close        -> reduce exposure by the full remaining qty
  (rebalance is a batch of the above sharing one batch_id; see `rebalance`)

All arithmetic is done in Decimal to match the Numeric(20,8) columns — never float.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Action, Position, PositionStatus, Side, TradeEvent

_ZERO = Decimal("0")
_INCREASE = {Action.open, Action.add}
_DECREASE = {Action.trim, Action.close}


def _dec(value: Any) -> Decimal:
    """Coerce to Decimal via str so float noise never enters the books."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_action(action: Action | str) -> Action:
    return action if isinstance(action, Action) else Action(str(action).lower())


def _as_side(side: Side | str) -> Side:
    return side if isinstance(side, Side) else Side(str(side).lower())


def _find_open_position(
    session: Session, symbol: str, side: Side
) -> Position | None:
    stmt = (
        select(Position)
        .where(
            Position.symbol == symbol,
            Position.side == side,
            Position.status == PositionStatus.open,
        )
        .order_by(Position.opened_at.desc())
    )
    return session.scalars(stmt).first()


def record_execution(
    session: Session,
    *,
    symbol: str,
    action: Action | str,
    qty: Any,
    price: Any,
    side: Side | str = Side.long,
    strategy_id: int | None = None,
    fees: Any = 0,
    ts: dt.datetime | None = None,
    batch_id: str | None = None,
    notes: str | None = None,
    position_id: int | None = None,
) -> TradeEvent:
    """Record one execution and update the affected position. Returns the TradeEvent.

    The session is flushed (not committed) so callers can compose multiple
    executions in one transaction (e.g. a rebalance).
    """
    symbol = symbol.strip().upper()
    act = _as_action(action)
    sd = _as_side(side)
    q = _dec(qty)
    px = _dec(price)
    fee = _dec(fees)
    when = ts or dt.datetime.now(dt.timezone.utc)

    if q <= _ZERO and act is not Action.close:
        raise ValueError("qty must be > 0")
    if px < _ZERO:
        raise ValueError("price must be >= 0")

    # Resolve the target position.
    if position_id is not None:
        pos = session.get(Position, position_id)
        if pos is None:
            raise ValueError(f"position {position_id} not found")
        if pos.status is PositionStatus.closed:
            raise ValueError(f"position {position_id} is closed")
    else:
        pos = _find_open_position(session, symbol, sd)

    if pos is None:
        if act in _DECREASE:
            raise ValueError(f"no open {sd.value} position for {symbol} to {act.value}")
        # open/add with no existing position -> create it
        pos = Position(
            symbol=symbol,
            side=sd,
            status=PositionStatus.open,
            qty=_ZERO,
            avg_entry_px=_ZERO,
            realized_pnl=_ZERO,
            strategy_id=strategy_id,
            opened_at=when,
        )
        session.add(pos)
        session.flush()

    q0 = _dec(pos.qty)
    avg0 = _dec(pos.avg_entry_px)
    realized = _ZERO

    if act in _INCREASE:
        new_qty = q0 + q
        # weighted-average entry
        pos.avg_entry_px = ((avg0 * q0) + (px * q)) / new_qty if new_qty > _ZERO else _ZERO
        pos.qty = new_qty
    else:  # trim / close
        closed_qty = q0 if act is Action.close else min(q, q0)
        if closed_qty <= _ZERO:
            raise ValueError(f"nothing to {act.value} on {symbol}")
        # long: profit when price rises; short: profit when price falls
        if pos.side is Side.long:
            realized = (px - avg0) * closed_qty
        else:
            realized = (avg0 - px) * closed_qty
        pos.qty = q0 - closed_qty
        # record the actual closed qty on the event
        q = closed_qty

    # Fees always reduce realized PnL on the event.
    realized = realized - fee
    pos.realized_pnl = _dec(pos.realized_pnl) + realized
    if strategy_id is not None and pos.strategy_id is None:
        pos.strategy_id = strategy_id

    if _dec(pos.qty) <= _ZERO:
        pos.qty = _ZERO
        pos.status = PositionStatus.closed
        pos.closed_at = when

    event = TradeEvent(
        position=pos,
        strategy_id=strategy_id if strategy_id is not None else pos.strategy_id,
        action=act,
        symbol=symbol,
        qty=q,
        price=px,
        fees=fee,
        realized_pnl=realized,
        batch_id=batch_id,
        ts=when,
        notes=notes,
    )
    session.add(event)
    session.flush()
    return event


def rebalance(
    session: Session,
    legs: Iterable[dict[str, Any]],
    *,
    note: str | None = None,
    ts: dt.datetime | None = None,
) -> list[TradeEvent]:
    """Apply a set of executions as one rebalance, tagged with a shared batch_id.

    Each leg is a dict accepted by record_execution (symbol, action, qty, price,
    optional side/strategy_id/fees/notes). All legs commit or roll back together.
    """
    batch_id = uuid.uuid4().hex
    when = ts or dt.datetime.now(dt.timezone.utc)
    events: list[TradeEvent] = []
    for leg in legs:
        leg = dict(leg)
        leg.setdefault("ts", when)
        leg.setdefault("notes", note)
        events.append(record_execution(session, batch_id=batch_id, **leg))
    return events


def open_positions(session: Session) -> list[Position]:
    stmt = (
        select(Position)
        .where(Position.status == PositionStatus.open)
        .order_by(Position.symbol)
    )
    return list(session.scalars(stmt))


def all_positions(
    session: Session, status: PositionStatus | str | None = None
) -> list[Position]:
    stmt = select(Position).order_by(Position.opened_at.desc())
    if status is not None:
        st = status if isinstance(status, PositionStatus) else PositionStatus(str(status))
        stmt = stmt.where(Position.status == st)
    return list(session.scalars(stmt))


def position_detail(session: Session, position_id: int) -> Position | None:
    return session.get(Position, position_id)


def ledger_summary(session: Session) -> dict[str, Any]:
    """Headline numbers for the ledger view."""
    opens = open_positions(session)
    all_pos = all_positions(session)
    total_realized = sum((_dec(p.realized_pnl) for p in all_pos), _ZERO)
    exposure = sum((_dec(p.qty) * _dec(p.avg_entry_px) for p in opens), _ZERO)
    return {
        "open_count": len(opens),
        "closed_count": sum(1 for p in all_pos if p.status is PositionStatus.closed),
        "total_realized_pnl": float(total_realized),
        "open_exposure": float(exposure),
    }
