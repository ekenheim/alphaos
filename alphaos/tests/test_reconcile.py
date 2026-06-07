"""Unit tests for cash reconciliation + the cash-flow ledger service.

`reconcile.account_cash` walks BOTH ledgers to derive the account's SEK cash
position: external deposits/withdrawals (signed amount_sek) from the cash-flow
ledger plus the cash impact of every buy/sell (stored signed Avanza Belopp when
present, otherwise quantity*price*fx minus fees). `account_position` turns that
net cash into loan / cash_asset. The cash-flow service mirrors the transactions
style (add/list/delete, a half-open net_flow_between window, and an idempotent
replace of the Avanza source range).

Everything runs against a fresh in-memory SQLite DB so the suite stays fast and
deterministic, and every numeric assertion is Decimal-aware.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import Base, Transaction, TransactionKind, TxnSource
from alphaos.db.config import get_config
from alphaos.db.reconcile import account_cash, account_position
from alphaos.db.cash_flows import (
    add_cash_flow,
    delete_cash_flow,
    list_cash_flows,
    net_flow_between,
    replace_avanza_cashflows,
)

_ISIN1 = "SE0000000001"
_ISIN2 = "SE0000000002"


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine)
    sess: Session = Maker()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _add_txn(session, *, date, isin, kind, qty, price,
             amount_sek=None, currency="SEK", fees=0):
    """Insert a ledger row directly (no holdings recompute) for cash-math tests."""
    txn = Transaction(
        date=dt.date.fromisoformat(date) if isinstance(date, str) else date,
        isin=isin,
        kind=kind if isinstance(kind, TransactionKind) else TransactionKind(kind),
        quantity=_dec(qty),
        price=_dec(price),
        currency=currency,
        amount_sek=None if amount_sek is None else _dec(amount_sek),
        fees_sek=_dec(fees),
        source=TxnSource.avanza,
    )
    session.add(txn)
    session.flush()
    return txn


# --------------------------------------------------------------------------- #
# reconcile.account_cash / account_position
# --------------------------------------------------------------------------- #

def test_account_cash_uses_signed_amounts_from_both_ledgers(session):
    # Deposit 100k, buy for 60k (Belopp -60000), sell for 20k (Belopp +20000).
    add_cash_flow(session, date="2026-01-01", amount_sek=100_000, kind="deposit")
    _add_txn(session, date="2026-01-05", isin=_ISIN1, kind="buy",
             qty=100, price=600, amount_sek=-60_000)
    _add_txn(session, date="2026-01-10", isin=_ISIN1, kind="sell",
             qty=20, price=1000, amount_sek=20_000)

    # net cash = 100_000 - 60_000 + 20_000 = 60_000.
    assert account_cash(session) == Decimal("60000")
    assert account_position(session) == {
        "net_cash": Decimal("60000"),
        "loan": Decimal("0"),
        "cash_asset": Decimal("60000"),
    }


def test_loan_when_buy_exceeds_deposit(session):
    # Buy more than has been deposited -> the account is overdrawn (a loan).
    add_cash_flow(session, date="2026-01-01", amount_sek=50_000, kind="deposit")
    _add_txn(session, date="2026-01-05", isin=_ISIN1, kind="buy",
             qty=100, price=700, amount_sek=-70_000)

    pos = account_position(session)
    # net cash = 50_000 - 70_000 = -20_000 -> loan 20_000, no idle cash asset.
    assert pos["net_cash"] == Decimal("-20000")
    assert pos["loan"] == Decimal("20000")           # loan = max(0, -net_cash)
    assert pos["cash_asset"] == Decimal("0")          # cash_asset = max(0, net_cash)


def test_withdrawal_reduces_cash(session):
    add_cash_flow(session, date="2026-01-01", amount_sek=100_000, kind="deposit")
    add_cash_flow(session, date="2026-02-01", amount_sek=30_000, kind="withdrawal")
    # Withdrawal stored negative -> net cash 70_000.
    assert account_cash(session) == Decimal("70000")


def test_multi_currency_and_fee_fallback(session):
    # No stored Belopp -> fall back to quantity*price*fx_to_sek(currency) minus fees,
    # with the sign coming from kind. Pin USD/SEK to 10 for clean arithmetic.
    cfg = get_config(session)
    cfg.fx_usd_sek = Decimal("10")
    session.flush()

    add_cash_flow(session, date="2026-01-01", amount_sek=20_000, kind="deposit")
    # USD buy, no amount_sek: -(10 * 100 * 10) - 50 = -10_050.
    _add_txn(session, date="2026-01-05", isin=_ISIN1, kind="buy",
             qty=10, price=100, currency="USD", fees=50, amount_sek=None)
    assert account_cash(session) == Decimal("9950")

    # SEK sell, no amount_sek: +(10 * 100 * 1) - 50 = +950.
    _add_txn(session, date="2026-01-06", isin=_ISIN2, kind="sell",
             qty=10, price=100, currency="SEK", fees=50, amount_sek=None)
    assert account_cash(session) == Decimal("10900")


def test_account_cash_through_is_inclusive(session):
    add_cash_flow(session, date="2026-01-10", amount_sek=1_000, kind="deposit")
    add_cash_flow(session, date="2026-02-10", amount_sek=2_000, kind="deposit")
    _add_txn(session, date="2026-01-15", isin=_ISIN1, kind="buy",
             qty=1, price=50, amount_sek=-50)

    # Strictly before the first deposit -> nothing yet.
    assert account_cash(session, through="2026-01-09") == Decimal("0")
    # The boundary date is INCLUDED.
    assert account_cash(session, through="2026-01-10") == Decimal("1000")
    # Through the buy date: 1000 - 50.
    assert account_cash(session, through="2026-01-15") == Decimal("950")
    # None = all-time: 1000 + 2000 - 50.
    assert account_cash(session) == Decimal("2950")


# --------------------------------------------------------------------------- #
# cash_flows service (sign-by-kind, half-open window, idempotent replace)
# --------------------------------------------------------------------------- #

def test_add_cash_flow_sets_sign_from_kind(session):
    dep = add_cash_flow(session, date="2026-01-01", amount_sek=1_000, kind="deposit")
    wd = add_cash_flow(session, date="2026-01-02", amount_sek=1_000, kind="withdrawal")
    assert _dec(dep.amount_sek) == Decimal("1000")
    assert _dec(wd.amount_sek) == Decimal("-1000")

    # An already-signed magnitude still normalizes via abs + kind.
    wd2 = add_cash_flow(session, date="2026-01-03", amount_sek=-500, kind="withdrawal")
    assert _dec(wd2.amount_sek) == Decimal("-500")

    assert {c.id for c in list_cash_flows(session)} == {dep.id, wd.id, wd2.id}
    assert delete_cash_flow(session, dep.id) is True
    assert delete_cash_flow(session, 9999) is False
    assert {c.id for c in list_cash_flows(session)} == {wd.id, wd2.id}


def test_net_flow_between_window_after_exclusive_through_inclusive(session):
    add_cash_flow(session, date="2026-01-31", amount_sek=100_000, kind="deposit")
    add_cash_flow(session, date="2026-02-15", amount_sek=50_000, kind="deposit")
    add_cash_flow(session, date="2026-02-28", amount_sek=25_000, kind="withdrawal")

    # (2026-01-31 EXCLUSIVE, 2026-02-28 INCLUSIVE]: +50_000 then -25_000;
    # the 2026-01-31 deposit is excluded (lower bound exclusive).
    assert net_flow_between(session, dt.date(2026, 1, 31), dt.date(2026, 2, 28)) == Decimal("25000")
    # Bounding before everything captures all three rows.
    assert net_flow_between(session, dt.date.min, dt.date(2026, 2, 28)) == Decimal("125000")
    # Empty window -> 0.
    assert net_flow_between(session, dt.date(2026, 2, 28), dt.date(2026, 3, 31)) == Decimal("0")


def test_replace_avanza_cashflows_is_idempotent_and_keeps_manual(session):
    # A manual flow inside the range must survive a re-import.
    add_cash_flow(session, date="2026-02-10", amount_sek=500, kind="deposit", source="manual")
    flows = [
        {"date": "2026-01-05", "amount_sek": Decimal("10000"), "kind": "deposit", "note": None},
        {"date": "2026-03-01", "amount_sek": Decimal("-2000"), "kind": "withdrawal", "note": None},
    ]
    assert replace_avanza_cashflows(session, flows, "2026-01-01", "2026-03-31") == 2
    # Re-running replaces the avanza rows in-range rather than appending.
    replace_avanza_cashflows(session, flows, "2026-01-01", "2026-03-31")

    all_cf = list_cash_flows(session)
    avanza = [c for c in all_cf if c.source is TxnSource.avanza]
    manual = [c for c in all_cf if c.source is TxnSource.manual]
    assert len(avanza) == 2            # not doubled
    assert len(manual) == 1            # manual untouched
    assert _dec(manual[0].amount_sek) == Decimal("500")
