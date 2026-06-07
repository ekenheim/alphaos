"""Unit tests for the DERIVED NAV ledger (the fully-derived snapshot path).

These complement test_nav.py (which drives add_snapshot with EXPLICIT measurements)
by exercising the derivation wiring instead:

  * add_snapshot leaves gross/loan/net_contribution = None and derives them —
    gross = securities @ latest close + idle cash; loan = account overdraw;
    net_contribution = cash-flow net over the period (prev EXCLUSIVE, this INCLUSIVE).
  * an explicitly-passed value always wins over its derivation.
  * upsert_snapshot replaces the row for a date (idempotent daily-job behaviour).
  * current_risk computes the CURRENT reading LIVE off today's closes against the
    latest persisted snapshot, without persisting a new row.

Everything runs against a fresh in-memory SQLite DB; assertions are Decimal-aware.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import Base, Transaction, TransactionKind, TxnSource
from alphaos.db.config import get_config
from alphaos.db.allocation import upsert_holding
from alphaos.db.cash_flows import add_cash_flow
from alphaos.db.nav import (
    add_snapshot,
    current_risk,
    list_snapshots,
    upsert_snapshot,
)

_ISIN1 = "SE0000000001"


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


def _add_txn(session, *, date, isin, kind, qty, price, amount_sek):
    """Insert a ledger row directly (no holdings recompute) so the buy's CASH
    impact is reconciled while the position's market value is set separately."""
    txn = Transaction(
        date=dt.date.fromisoformat(date) if isinstance(date, str) else date,
        isin=isin,
        kind=kind if isinstance(kind, TransactionKind) else TransactionKind(kind),
        quantity=_dec(qty),
        price=_dec(price),
        amount_sek=_dec(amount_sek),
        source=TxnSource.avanza,
    )
    session.add(txn)
    session.flush()
    return txn


# --------------------------------------------------------------------------- #
# add_snapshot — derivation of gross / loan / net_contribution
# --------------------------------------------------------------------------- #

def test_derived_gross_includes_securities_plus_idle_cash(session):
    # Deposit 1,000,000; spend 600,000 on a position now worth 700,000.
    add_cash_flow(session, date="2026-01-02", amount_sek=1_000_000, kind="deposit")
    _add_txn(session, date="2026-01-03", isin=_ISIN1, kind="buy",
             qty=1000, price=600, amount_sek=-600_000)
    upsert_holding(session, isin=_ISIN1, symbol="ALPH",
                   quantity=1000, last_price=700, currency="SEK")

    snap = add_snapshot(session, as_of="2026-01-31")

    # gross = securities (1000 * 700 = 700_000) + idle cash (1_000_000 - 600_000).
    assert _dec(snap.gross_asset_value) == Decimal("1100000")
    assert _dec(snap.loan_balance) == Decimal("0")
    assert _dec(snap.equity) == Decimal("1100000")
    # First snapshot: net_contribution captures every flow through as_of.
    assert _dec(snap.net_contribution) == Decimal("1000000")


def test_derived_loan_when_buy_exceeds_deposit(session):
    # Deposit only 500,000 but buy 600,000 -> account overdrawn by 100,000.
    add_cash_flow(session, date="2026-01-02", amount_sek=500_000, kind="deposit")
    _add_txn(session, date="2026-01-03", isin=_ISIN1, kind="buy",
             qty=1000, price=600, amount_sek=-600_000)
    upsert_holding(session, isin=_ISIN1, symbol="ALPH",
                   quantity=1000, last_price=700, currency="SEK")

    snap = add_snapshot(session, as_of="2026-01-31")

    assert _dec(snap.loan_balance) == Decimal("100000")   # = max(0, -net_cash)
    # No idle cash -> gross is securities only.
    assert _dec(snap.gross_asset_value) == Decimal("700000")
    assert _dec(snap.equity) == Decimal("600000")


def test_explicit_values_override_derivation(session):
    # Ledger would derive gross=1_000_000 / loan=0, but explicit args must win.
    add_cash_flow(session, date="2026-01-02", amount_sek=1_000_000, kind="deposit")
    upsert_holding(session, isin=_ISIN1, symbol="ALPH",
                   quantity=1000, last_price=700, currency="SEK")

    snap = add_snapshot(
        session,
        as_of="2026-01-31",
        gross_asset_value=2_000_000,
        loan_balance=50_000,
        net_contribution=12_345,
    )

    assert _dec(snap.gross_asset_value) == Decimal("2000000")
    assert _dec(snap.loan_balance) == Decimal("50000")
    assert _dec(snap.net_contribution) == Decimal("12345")
    assert _dec(snap.equity) == Decimal("1950000")


def test_net_contribution_window_prev_exclusive_this_inclusive(session):
    # First snapshot: a deposit on the snapshot date counts (no prior bound).
    add_cash_flow(session, date="2026-01-31", amount_sek=100_000, kind="deposit")
    snap1 = add_snapshot(session, as_of="2026-01-31")
    assert _dec(snap1.net_contribution) == Decimal("100000")

    # Two more deposits within the next period.
    add_cash_flow(session, date="2026-02-15", amount_sek=50_000, kind="deposit")
    add_cash_flow(session, date="2026-02-28", amount_sek=25_000, kind="deposit")
    snap2 = add_snapshot(session, as_of="2026-02-28")

    # Window (snap1.as_of EXCLUSIVE, snap2.as_of INCLUSIVE]: the 2026-01-31 deposit
    # is excluded; 2026-02-15 and the boundary 2026-02-28 deposit are included.
    assert _dec(snap2.net_contribution) == Decimal("75000")


# --------------------------------------------------------------------------- #
# upsert_snapshot — idempotent replace for a given trading day
# --------------------------------------------------------------------------- #

def test_upsert_snapshot_is_idempotent_and_reflects_new_data(session):
    add_cash_flow(session, date="2026-01-31", amount_sek=1_000_000, kind="deposit")

    s1 = upsert_snapshot(session, as_of="2026-01-31")
    assert len(list_snapshots(session)) == 1
    assert _dec(s1.gross_asset_value) == Decimal("1000000")

    # Re-running the day's job replaces rather than appends.
    s2 = upsert_snapshot(session, as_of="2026-01-31")
    assert len(list_snapshots(session)) == 1
    assert _dec(s2.gross_asset_value) == Decimal("1000000")

    # New data on the SAME day -> the single row is re-derived.
    add_cash_flow(session, date="2026-01-31", amount_sek=200_000, kind="deposit")
    s3 = upsert_snapshot(session, as_of="2026-01-31")
    assert len(list_snapshots(session)) == 1
    assert _dec(s3.gross_asset_value) == Decimal("1200000")


# --------------------------------------------------------------------------- #
# current_risk — live reading off today's closes (no persistence)
# --------------------------------------------------------------------------- #

def test_current_risk_empty_state(session):
    risk = current_risk(session)

    assert risk["as_of"] is None
    assert risk["nav_index"] is None
    assert risk["delever_status"] == "normal"
    assert risk["action"] == "no NAV snapshots yet"
    # The glide target is still reported in the empty state.
    assert risk["target_leverage"] == pytest.approx(float(get_config(session).leverage_target))


def test_current_risk_live_derivation_against_prior_snapshot(session):
    # A persisted baseline strictly before today (explicit so it's deterministic).
    prev_date = dt.date.today() - dt.timedelta(days=30)
    add_snapshot(session, as_of=prev_date, gross_asset_value=1_000_000, loan_balance=0)

    # Today's ledger: deposit 1,000,000, buy a position for 1,000,000 (net cash 0),
    # the position has since risen to 1,200,000.
    add_cash_flow(session, date=prev_date, amount_sek=1_000_000, kind="deposit")
    _add_txn(session, date=prev_date, isin=_ISIN1, kind="buy",
             qty=1000, price=1000, amount_sek=-1_000_000)
    upsert_holding(session, isin=_ISIN1, symbol="ALPH",
                   quantity=1000, last_price=1200, currency="SEK")

    risk = current_risk(session)

    # Live gross = securities 1,200,000 + idle cash 0; equity = gross (no loan).
    assert risk["as_of"] == dt.date.today().isoformat()
    assert risk["gross_asset_value"] == pytest.approx(1_200_000.0)
    assert risk["loan_balance"] == pytest.approx(0.0)
    assert risk["equity"] == pytest.approx(1_200_000.0)
    # twr vs the baseline equity (contribution forced to 0 in the live path):
    # (1_200_000 - 1_000_000) / 1_000_000 = 0.20 -> nav_index 1.20, no drawdown.
    assert risk["twr_period"] == pytest.approx(0.20)
    assert risk["nav_index"] == pytest.approx(1.20)
    assert risk["drawdown"] == pytest.approx(0.0)
    assert risk["delever_status"] == "normal"
    # The live reading must NOT persist a new snapshot.
    assert len(list_snapshots(session)) == 1


def test_current_risk_live_breaches_half_delever(session):
    prev_date = dt.date.today() - dt.timedelta(days=30)
    add_snapshot(session, as_of=prev_date, gross_asset_value=1_000_000, loan_balance=0)

    # Today equity is only 640,000 (idle cash, no holdings) -> -36% vs baseline.
    add_cash_flow(session, date=prev_date, amount_sek=640_000, kind="deposit")

    risk = current_risk(session)

    assert risk["drawdown"] == pytest.approx(-0.36)
    assert risk["nav_index"] == pytest.approx(0.64)
    assert risk["delever_status"] == "half"
    assert "HALF" in risk["action"]
    assert len(list_snapshots(session)) == 1
