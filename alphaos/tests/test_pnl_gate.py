"""Money-terms P&L + NAV-index reliability gate.

portfolio_pnl answers 'am I up or down vs what I paid' from current price vs cost,
needing no contribution history. current_risk additionally flags the time-weighted
NAV index as unreliable (so the UI shows '—') when contributions are under-recorded
or there is too little real history.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.allocation import portfolio_pnl, upsert_holding
from alphaos.db.cash_flows import add_cash_flow
from alphaos.db.models import Base, CashFlowKind, PriceSource, Transaction, TransactionKind, TxnSource
from alphaos.db.nav import current_risk


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


def _holding(session, **kw):
    upsert_holding(session, currency="SEK", **kw)


def test_pnl_gain_and_source_counts(session):
    # Bought 100 @ 1000 (cost 100k), now 1100 -> +10,000 (+10%); one live, one at cost.
    _holding(session, symbol="AAA", isin="SE0000000001", quantity=100,
             avg_price=1000, last_price=1100, price_source=PriceSource.minio)
    _holding(session, symbol="BBB", isin="SE0000000002", quantity=10,
             avg_price=500, price_source=PriceSource.cost)  # no last_price -> at cost

    pnl = portfolio_pnl(session)
    assert pnl["cost_basis"] == Decimal("105000")
    assert pnl["market_value"] == Decimal("115000")
    assert pnl["unrealized_pnl"] == Decimal("10000")
    assert pnl["return_pct"] == pytest.approx(Decimal("10000") / Decimal("105000"))
    assert pnl["priced"] == 1
    assert pnl["at_cost"] == 1


def test_nav_index_gated_when_contributions_incomplete(session):
    # 100k of holdings but only a 10k deposit recorded -> index not trustworthy.
    _holding(session, symbol="AAA", isin="SE0000000001", quantity=100,
             avg_price=1000, last_price=1000, price_source=PriceSource.minio)
    session.add(Transaction(
        date=dt.date(2026, 6, 1), isin="SE0000000001", kind=TransactionKind.buy,
        quantity=Decimal("100"), price=Decimal("1000"), amount_sek=Decimal("-100000"),
        source=TxnSource.avanza,
    ))
    add_cash_flow(session, date="2026-06-01", amount_sek=10_000,
                  kind=CashFlowKind.deposit, source=TxnSource.manual)
    session.flush()

    risk = current_risk(session)
    assert risk["nav_index_reliable"] is False
    assert risk["nav_index_note"]  # a human-readable reason is present
    # P&L is always populated regardless of the index gate.
    assert risk["pnl"]["cost_basis"] == pytest.approx(100000.0)
