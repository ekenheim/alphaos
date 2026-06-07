"""Cash-flow ledger services — external deposits/withdrawals.

One row per cash movement (deposit +, withdrawal -). A NAV snapshot's
net_contribution is DERIVED from these (net_flow_between) over the snapshot's
period, so adding a snapshot needs no manual contribution entry. Instrument
holdings are unaffected — those derive from the transaction ledger.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import CashFlow, CashFlowKind, TxnSource

_ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_kind(k: CashFlowKind | str) -> CashFlowKind:
    return k if isinstance(k, CashFlowKind) else CashFlowKind(str(k).lower())


def _as_source(s: TxnSource | str) -> TxnSource:
    return s if isinstance(s, TxnSource) else TxnSource(str(s).lower())


def _as_date(d: dt.date | str) -> dt.date:
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))


def list_cash_flows(session: Session, limit: int | None = None) -> list[CashFlow]:
    stmt = select(CashFlow).order_by(CashFlow.date, CashFlow.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def add_cash_flow(
    session: Session,
    *,
    date: dt.date | str,
    amount_sek: Any,
    kind: CashFlowKind | str,
    note: str | None = None,
    source: TxnSource | str = TxnSource.manual,
) -> CashFlow:
    """Record a deposit/withdrawal. `amount_sek` is an unsigned magnitude; the
    kind sets the stored sign (deposit +, withdrawal -)."""
    k = _as_kind(kind)
    amount = abs(_dec(amount_sek))
    if amount <= _ZERO:
        raise ValueError("amount_sek must be > 0")
    signed = amount if k is CashFlowKind.deposit else -amount
    cf = CashFlow(
        date=_as_date(date), amount_sek=signed, kind=k,
        source=_as_source(source), note=note,
    )
    session.add(cf)
    session.flush()
    return cf


def delete_cash_flow(session: Session, cf_id: int) -> bool:
    cf = session.get(CashFlow, cf_id)
    if cf is None:
        return False
    session.delete(cf)
    session.flush()
    return True


def net_flow_between(session: Session, after: dt.date, through: dt.date) -> Decimal:
    """Sum of signed amount_sek for `after < date <= through`
    (after EXCLUSIVE, through INCLUSIVE). 0 when no rows."""
    total = session.scalar(
        select(func.coalesce(func.sum(CashFlow.amount_sek), 0)).where(
            CashFlow.date > after,
            CashFlow.date <= through,
        )
    )
    return _dec(total)


def replace_avanza_cashflows(
    session: Session,
    flows: list[dict[str, Any]],
    date_min: dt.date | str,
    date_max: dt.date | str,
) -> int:
    """Replace all source='avanza' cash flows in [date_min, date_max] with `flows`.

    Makes re-importing a full Avanza export idempotent. Manual flows are untouched.
    `amount_sek` in each flow is ALREADY signed (deposit +, withdrawal -).
    """
    dmin, dmax = _as_date(date_min), _as_date(date_max)
    session.execute(
        delete(CashFlow).where(
            CashFlow.source == TxnSource.avanza,
            CashFlow.date >= dmin,
            CashFlow.date <= dmax,
        )
    )
    for f in flows:
        session.add(CashFlow(
            date=_as_date(f["date"]),
            amount_sek=_dec(f["amount_sek"]),
            kind=_as_kind(f["kind"]),
            source=TxnSource.avanza,
            note=f.get("note"),
        ))
    session.flush()
    return len(flows)
