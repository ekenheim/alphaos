"""Cash reconciliation — derive the account's SEK cash position from BOTH ledgers.

Read-only. The SEK cash balance is: external deposits (+) / withdrawals (-) from
the cash-flow ledger, plus the cash impact of every buy/sell in the transaction
ledger (a buy reduces cash by its full SEK cost incl. fees; a sell increases it by
its proceeds net of fees). The stored signed Avanza 'Belopp' (amount_sek) already
nets courtage and carries the sign, so it is preferred; otherwise we fall back to
quantity * price * fx_to_sek with the sign from kind, minus fees.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_config
from .fx import fx_to_sek
from .models import CashFlow, Transaction, TransactionKind

_ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_date(value: dt.date | str) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _txn_cash_delta(cfg, t: Transaction) -> Decimal:
    """Signed SEK cash impact of one transaction (buy < 0, sell > 0)."""
    if t.amount_sek is not None:
        return _dec(t.amount_sek)  # Avanza Belopp: signed, already nets courtage
    base = _dec(t.quantity) * _dec(t.price) * fx_to_sek(cfg, t.currency)
    fees = _dec(t.fees_sek or 0)
    if t.kind is TransactionKind.buy:
        return -base - fees
    return base - fees


def account_cash(session: Session, through: dt.date | None = None) -> Decimal:
    """SEK cash balance through `through` (INCLUSIVE; None = all-time)."""
    cfg = get_config(session)
    cf_stmt = select(func.coalesce(func.sum(CashFlow.amount_sek), 0))
    tx_stmt = select(Transaction)
    if through is not None:
        thru = _as_date(through)
        cf_stmt = cf_stmt.where(CashFlow.date <= thru)
        tx_stmt = tx_stmt.where(Transaction.date <= thru)
    total = _dec(session.scalar(cf_stmt))
    for t in session.scalars(tx_stmt):
        total += _txn_cash_delta(cfg, t)
    return total


def account_position(session: Session, through: dt.date | None = None) -> dict[str, Decimal]:
    """{net_cash, loan = max(0,-net_cash), cash_asset = max(0,net_cash)} — all Decimal."""
    net = account_cash(session, through)
    return {"net_cash": net, "loan": max(_ZERO, -net), "cash_asset": max(_ZERO, net)}
