"""Tests for the historical NAV reconstruction (alphaos.db.history).

The past is rebuilt from the transaction ledger + per-day closes: holdings are
re-aggregated as of each day, valued at that day's close, netted against the
reconciled cash position, and chained through the shared per-period math. Closes
are INJECTED here so the tests never touch MinIO.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db import history as dbhistory
from alphaos.db.cash_flows import add_cash_flow
from alphaos.db.history import backfill_snapshots, reconstruct_series
from alphaos.db.models import Base, CashFlowKind, Transaction, TransactionKind, TxnSource
from alphaos.db.nav import list_snapshots

_ISIN = "US0378331005"
_SYM = "AAPL"


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


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


def _seed(session):
    """100 shares bought for 100,000 SEK, funded by a 100,000 deposit -> no loan."""
    add_cash_flow(
        session, date="2025-05-31", amount_sek=100_000,
        kind=CashFlowKind.deposit, source=TxnSource.manual,
    )
    session.add(Transaction(
        date=dt.date(2025, 6, 2), isin=_ISIN, symbol=_SYM, currency="SEK",
        kind=TransactionKind.buy, quantity=_dec(100), price=_dec(1000),
        amount_sek=_dec(-100_000), source=TxnSource.avanza,
    ))
    session.flush()


# Close goes 1000 -> 1100 (+10%) over the window; SEK so no FX.
_CLOSES = {
    dt.date(2025, 6, 2): {_SYM: Decimal("1000")},
    dt.date(2025, 6, 6): {_SYM: Decimal("1100")},
}


def test_reconstructs_daily_series_with_price_gain(session):
    _seed(session)
    rows = reconstruct_series(session, closes=_CLOSES, end="2025-06-06")

    assert [r["as_of"] for r in rows] == ["2025-06-02", "2025-06-06"]
    # First point baselines the index; no loan because the deposit funded the buy.
    assert rows[0]["nav_index"] == pytest.approx(1.0)
    assert rows[0]["twr_period"] is None
    assert rows[0]["loan_balance"] == pytest.approx(0.0)
    assert rows[0]["equity"] == pytest.approx(100_000.0)
    # +10% close with zero contribution that period -> NAV 1.10, TWR 0.10.
    assert rows[1]["twr_period"] == pytest.approx(0.10)
    assert rows[1]["nav_index"] == pytest.approx(1.10)
    assert rows[1]["drawdown"] == pytest.approx(0.0)
    assert rows[1]["equity"] == pytest.approx(110_000.0)


def test_drawdown_when_price_falls_back(session):
    _seed(session)
    closes = dict(_CLOSES)
    closes[dt.date(2025, 6, 9)] = {_SYM: Decimal("990")}  # -10% off the 1100 peak
    rows = reconstruct_series(session, closes=closes, end="2025-06-09")

    last = rows[-1]
    assert last["as_of"] == "2025-06-09"
    assert last["peak_nav_index"] == pytest.approx(1.10)
    assert last["nav_index"] == pytest.approx(0.99)
    assert last["drawdown"] == pytest.approx(0.99 / 1.10 - 1.0)  # ~ -0.10


def test_empty_without_price_history(session):
    _seed(session)
    assert reconstruct_series(session, closes={}) == []


def test_no_transactions_returns_empty(session):
    assert reconstruct_series(session, closes=_CLOSES) == []


def test_backfill_persists_series(session, monkeypatch):
    _seed(session)
    # backfill calls reconstruct_series WITHOUT injected closes -> stub the fetch.
    monkeypatch.setattr(
        dbhistory.pricing, "closes_in_range", lambda *a, **k: _CLOSES
    )
    written = backfill_snapshots(session, end="2025-06-06")
    assert written == 2

    snaps = list_snapshots(session)
    assert [s.as_of.isoformat() for s in snaps] == ["2025-06-02", "2025-06-06"]
    assert float(snaps[-1].nav_index) == pytest.approx(1.10)
    assert all(s.notes == "reconstructed" for s in snaps)

    # Idempotent: a second run replaces the range rather than duplicating.
    assert backfill_snapshots(session, end="2025-06-06") == 2
    assert len(list_snapshots(session)) == 2
