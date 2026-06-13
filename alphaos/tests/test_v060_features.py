"""Tests for the v0.6.0 dashboard refinements:

  * update_transaction (edit a ledger row, recompute the holding)
  * delete_sleeve (PRESERVES holdings -> Unassigned; records history)
  * upsert_sleeve weight-change history
  * seed_default_sleeves is a one-time bootstrap (no resurrection)
  * FX history upsert-by-date
  * compute_cagr_since_inception (TWR-annualized, reliability-gated)

All against a fresh in-memory SQLite DB.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import Base, NavSnapshot
from alphaos.db import allocation as alloc
from alphaos.db import config as cfgmod
from alphaos.db import fx as fxmod
from alphaos.db import nav
from alphaos.db import transactions as tx

ISIN = "US0000000001"


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


def test_update_transaction_recomputes_and_validates(session):
    t = tx.add_transaction(session, date="2026-01-10", isin=ISIN, kind="buy",
                           quantity=100, price=50, source="manual")
    assert float(alloc.get_holding_by_isin(session, ISIN).quantity) == 100

    out = tx.update_transaction(session, t.id, quantity=40, price=55)
    assert out is not None
    assert float(alloc.get_holding_by_isin(session, ISIN).quantity) == 40

    assert tx.update_transaction(session, 999999, quantity=1) is None  # missing -> None
    with pytest.raises(ValueError):
        tx.update_transaction(session, t.id, quantity=0)               # qty must be > 0


def test_delete_sleeve_preserves_holdings_and_records_history(session):
    s = alloc.upsert_sleeve(session, "RAW", name="RAW", target_weight=Decimal("0.45"))
    alloc.upsert_holding(session, sleeve_code="RAW", symbol="NVDA", isin=ISIN,
                         quantity=10, avg_price=100)

    assert alloc.delete_sleeve(session, s.id) is True

    h = alloc.get_holding_by_isin(session, ISIN)
    assert h is not None             # holding preserved
    assert h.sleeve_id is None       # detached to Unassigned
    assert float(h.quantity) == 10

    events = [(x.sleeve_code, x.event) for x in alloc.list_sleeve_weight_history(session)]
    assert ("RAW", "created") in events
    assert ("RAW", "deleted") in events
    assert alloc.delete_sleeve(session, 999999) is False               # missing -> False


def test_upsert_sleeve_records_only_real_weight_changes(session):
    alloc.upsert_sleeve(session, "VVSM", name="VVSM", target_weight=Decimal("0.11"))
    alloc.upsert_sleeve(session, "VVSM", target_weight=Decimal("0.25"))   # updated
    alloc.upsert_sleeve(session, "VVSM", target_weight=Decimal("0.25"))   # no change

    events = [r.event for r in alloc.list_sleeve_weight_history(session, code="VVSM")]
    assert events.count("created") == 1
    assert events.count("updated") == 1


def test_seed_is_bootstrap_once(session):
    cfgmod.get_config(session)
    assert alloc.seed_default_sleeves(session) == 5
    assert alloc.seed_default_sleeves(session) == 0                       # no-op

    cndx = alloc.get_sleeve(session, "CNDX")
    alloc.delete_sleeve(session, cndx.id)
    assert alloc.seed_default_sleeves(session) == 0                       # does not resurrect
    assert alloc.get_sleeve(session, "CNDX") is None


def test_fx_history_upserts_by_date(session):
    fxmod._record_fx_history(session, dt.date(2026, 6, 12), Decimal("9.30"), Decimal("10.80"), "riksbank")
    fxmod._record_fx_history(session, dt.date(2026, 6, 13), Decimal("9.40"), Decimal("10.90"), "riksbank")
    fxmod._record_fx_history(session, dt.date(2026, 6, 13), Decimal("9.45"), Decimal("10.95"), "ecb")  # upsert

    hist = fxmod.list_fx_history(session)
    assert len(hist) == 2
    assert hist[0].as_of == dt.date(2026, 6, 13)   # most-recent-first
    assert float(hist[0].usd_sek) == 9.45
    assert hist[0].source == "ecb"


def test_cagr_since_inception(session):
    assert nav.compute_cagr_since_inception(session, 1.25, dt.date(2026, 6, 13), reliable=True) == (None, None)

    session.add(NavSnapshot(as_of=dt.date(2025, 6, 13), gross_asset_value=Decimal("100"),
                            equity=Decimal("100"), nav_index=Decimal("1.0"), peak_nav_index=Decimal("1.0")))
    session.add(NavSnapshot(as_of=dt.date(2026, 6, 13), gross_asset_value=Decimal("125"),
                            equity=Decimal("125"), nav_index=Decimal("1.25"), peak_nav_index=Decimal("1.25")))
    session.flush()

    cagr, inc = nav.compute_cagr_since_inception(session, 1.25, dt.date(2026, 6, 13), reliable=True)
    assert inc == "2025-06-13"
    assert cagr == pytest.approx(0.25, abs=2e-3)                          # ~1yr, +25%

    cagr2, inc2 = nav.compute_cagr_since_inception(session, 1.25, dt.date(2026, 6, 13), reliable=False)
    assert cagr2 is None and inc2 == "2025-06-13"                         # gated when unreliable
