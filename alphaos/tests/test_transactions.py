"""Unit tests for the transaction ledger services.

Holdings are DERIVED from the ledger: aggregation uses the average-cost method,
recompute rebuilds the derived holding fields while preserving metadata, and the
add/delete mutations recompute the affected position. Every test runs against a
fresh in-memory SQLite DB so the suite stays fast and deterministic, and all
numeric assertions are Decimal-aware.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import Base, Transaction, TransactionKind, TxnSource
from alphaos.db.allocation import get_holding_by_isin, upsert_holding, upsert_sleeve
from alphaos.db.transactions import (
    add_transaction,
    aggregate,
    delete_transaction,
    list_transactions,
    position_history,
    recompute_holdings,
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


def _txn_dict(date, isin, kind, qty, price, amount_sek, *, name=None):
    return {
        "date": date,
        "isin": isin,
        "name": name,
        "currency": "SEK",
        "kind": kind,
        "quantity": _dec(qty),
        "price": _dec(price),
        "amount_sek": _dec(amount_sek),
    }


# --------------------------------------------------------------------------- #
# aggregate (pure, average-cost)
# --------------------------------------------------------------------------- #

def test_aggregate_buy_then_partial_sell():
    # ISIN1: buy 100@50 (-5000), buy 100@70 (-7000) -> 200 @ avg 60, then sell 50.
    # ISIN2: buy 10@100 then sell all 10 -> fully closed, excluded.
    txns = [
        _txn_dict("2026-01-10", _ISIN1, "buy", 100, 50, -5000, name="Alpha AB"),
        _txn_dict("2026-02-15", _ISIN1, "buy", 100, 70, -7000),
        _txn_dict("2026-03-20", _ISIN1, "sell", 50, 80, 4000),
        _txn_dict("2026-03-01", _ISIN2, "buy", 10, 100, -1000, name="Beta AB"),
        _txn_dict("2026-04-10", _ISIN2, "sell", 10, 110, 1100),
    ]

    agg = aggregate(txns)

    # Fully-sold ISIN2 is not an open position.
    assert set(agg) == {_ISIN1}
    a = agg[_ISIN1]
    # Net qty = 200 - 50 = 150.
    assert a["qty"] == Decimal("150")
    # avg_price is the weighted avg of the BUYS (60), unchanged by the sell.
    assert a["avg_price"] == Decimal("60")
    # Cost basis reduced proportionally: 12000 * (150/200) = 9000.
    assert a["cost_sek"] == Decimal("9000")
    # acquired_at is the earliest buy.
    assert a["acquired_at"] == dt.date(2026, 1, 10)
    assert a["name"] == "Alpha AB"


# --------------------------------------------------------------------------- #
# recompute_holdings (derives fields, preserves metadata)
# --------------------------------------------------------------------------- #

def test_recompute_derives_fields_and_preserves_metadata(session):
    # Seed a holding with metadata (sleeve + ticker) BEFORE any transactions.
    sleeve = upsert_sleeve(session, "RAW", name="RAW", target_weight=Decimal("0.45"))
    upsert_holding(
        session,
        sleeve_id=sleeve.id,
        symbol="ALPH",
        isin=_ISIN1,
        name="Alpha AB",
    )

    # Insert ledger rows directly, then recompute the derived fields.
    session.add_all([
        Transaction(date=dt.date(2026, 1, 10), isin=_ISIN1, kind=TransactionKind.buy,
                    quantity=Decimal("100"), price=Decimal("50"),
                    amount_sek=Decimal("-5000"), source=TxnSource.avanza),
        Transaction(date=dt.date(2026, 2, 15), isin=_ISIN1, kind=TransactionKind.buy,
                    quantity=Decimal("100"), price=Decimal("70"),
                    amount_sek=Decimal("-7000"), source=TxnSource.avanza),
        Transaction(date=dt.date(2026, 3, 20), isin=_ISIN1, kind=TransactionKind.sell,
                    quantity=Decimal("50"), price=Decimal("80"),
                    amount_sek=Decimal("4000"), source=TxnSource.avanza),
    ])
    session.flush()

    recompute_holdings(session, isins=[_ISIN1])

    h = get_holding_by_isin(session, _ISIN1)
    # Metadata kept.
    assert h.sleeve_id == sleeve.id
    assert h.symbol == "ALPH"
    # Derived fields updated from the ledger.
    assert _dec(h.quantity) == Decimal("150")
    assert _dec(h.avg_price) == Decimal("60")
    assert _dec(h.cost_basis_sek) == Decimal("9000")
    assert h.acquired_at == dt.date(2026, 1, 10)


# --------------------------------------------------------------------------- #
# add_transaction / delete_transaction (each recomputes)
# --------------------------------------------------------------------------- #

def test_add_sell_reduces_then_delete_restores_holding(session):
    add_transaction(
        session, date="2026-01-10", isin=_ISIN1, kind="buy",
        quantity=100, price=50, amount_sek=-5000, name="Alpha AB", source="manual",
    )
    h = get_holding_by_isin(session, _ISIN1)
    assert _dec(h.quantity) == Decimal("100")
    assert _dec(h.cost_basis_sek) == Decimal("5000")

    # A sell reduces the derived holding (recompute runs inside add_transaction).
    sell = add_transaction(
        session, date="2026-02-01", isin=_ISIN1, kind="sell",
        quantity=30, price=60, amount_sek=1800, source="manual",
    )
    h = get_holding_by_isin(session, _ISIN1)
    assert _dec(h.quantity) == Decimal("70")
    # Cost basis falls proportionally: 5000 * (70/100) = 3500.
    assert _dec(h.cost_basis_sek) == Decimal("3500")

    # Deleting the sell restores the position (delete also recomputes).
    assert delete_transaction(session, sell.id) is True
    h = get_holding_by_isin(session, _ISIN1)
    assert _dec(h.quantity) == Decimal("100")
    assert _dec(h.cost_basis_sek) == Decimal("5000")
    # Only the original buy remains in the ledger.
    assert len(list_transactions(session, isin=_ISIN1)) == 1


# --------------------------------------------------------------------------- #
# position_history (running quantity, chronological)
# --------------------------------------------------------------------------- #

def test_position_history_running_qty_ordered_by_date(session):
    # Insert out of date order to prove the history is sorted by date.
    add_transaction(session, date="2026-03-20", isin=_ISIN1, kind="sell",
                    quantity=50, price=80, amount_sek=4000, source="manual")
    add_transaction(session, date="2026-01-10", isin=_ISIN1, kind="buy",
                    quantity=100, price=50, amount_sek=-5000, source="manual")
    add_transaction(session, date="2026-02-15", isin=_ISIN1, kind="buy",
                    quantity=100, price=70, amount_sek=-7000, source="manual")

    hist = position_history(session, _ISIN1)

    # Chronological order.
    dates = [r["date"] for r in hist]
    assert dates == ["2026-01-10", "2026-02-15", "2026-03-20"]

    # running_qty == cumulative buys minus sells.
    kinds = [r["kind"] for r in hist]
    assert kinds == ["buy", "buy", "sell"]
    assert [r["running_qty"] for r in hist] == [100.0, 200.0, 150.0]

    # Manual rebalance leg is tagged as the manual source.
    assert all(r["source"] == "manual" for r in hist)
