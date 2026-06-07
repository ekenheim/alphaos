"""Daily money journal + per-day holdings (powers the heatmap, feed, and expand).

Closes are injected so MinIO is never touched. Sleeve filtering, day-over-day P&L,
buy/sell events, and the per-date holdings breakdown are all asserted here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.allocation import upsert_holding, upsert_sleeve
from alphaos.db.history import daily_journal, holdings_on
from alphaos.db.models import Base, SleeveKind, Transaction, TransactionKind, TxnSource

_ISIN = "US0378331005"
_SYM = "AAPL"
_ISIN_OFF = "US5949181045"  # held but NOT assigned to a sleeve
_SYM_OFF = "MSFT"


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
    upsert_sleeve(session, "CORE", name="Core", kind=SleeveKind.beta_core)
    # AAPL is in a sleeve; MSFT is held but unassigned (excluded when sleeve_only).
    upsert_holding(session, sleeve_code="CORE", symbol=_SYM, isin=_ISIN, currency="SEK")
    upsert_holding(session, symbol=_SYM_OFF, isin=_ISIN_OFF, currency="SEK")
    for isin, sym, qty, px in [(_ISIN, _SYM, 100, 1000), (_ISIN_OFF, _SYM_OFF, 50, 200)]:
        session.add(Transaction(
            date=dt.date(2025, 6, 2), isin=isin, symbol=sym, currency="SEK",
            kind=TransactionKind.buy, quantity=Decimal(qty), price=Decimal(px),
            amount_sek=Decimal(-qty * px), source=TxnSource.avanza,
        ))
    session.flush()


_CLOSES = {
    dt.date(2025, 6, 2): {_SYM: Decimal("1000"), _SYM_OFF: Decimal("200")},
    dt.date(2025, 6, 3): {_SYM: Decimal("1100"), _SYM_OFF: Decimal("100")},  # AAPL +10%, MSFT -50%
}


def test_journal_is_sleeve_only_and_daily(session):
    _seed(session)
    rows = daily_journal(session, end="2025-06-03", closes=_CLOSES)

    # One row per calendar day from the first transaction to end (2nd, 3rd).
    assert [r["date"] for r in rows] == ["2025-06-02", "2025-06-03"]
    # Only AAPL (sleeved) counts: cost 100k, value 100k -> day1 flat.
    assert rows[0]["cost_basis"] == pytest.approx(100_000.0)
    assert rows[0]["value"] == pytest.approx(100_000.0)
    assert rows[0]["pnl"] == pytest.approx(0.0)
    assert rows[0]["has_event"] is True          # the buy happened this day
    assert "+AAPL" in rows[0]["event"]
    assert "MSFT" not in (rows[0]["event"] or "")  # unsleeved name excluded
    # Day 2: AAPL +10% -> +10k; MSFT's −50% is ignored (not in a sleeve).
    assert rows[1]["value"] == pytest.approx(110_000.0)
    assert rows[1]["day_pnl"] == pytest.approx(10_000.0)


def test_journal_includes_unsleeved_when_flag_off(session):
    _seed(session)
    rows = daily_journal(session, end="2025-06-03", sleeve_only=False, closes=_CLOSES)
    # Day1 value = AAPL 100k + MSFT 10k = 110k.
    assert rows[0]["value"] == pytest.approx(110_000.0)


def test_holdings_on_returns_positions_with_valuation(session):
    _seed(session)
    rows = holdings_on(session, "2025-06-03", closes=_CLOSES)
    assert len(rows) == 1                          # sleeve-only -> AAPL only
    h = rows[0]
    assert h["symbol"] == _SYM
    assert h["quantity"] == pytest.approx(100.0)
    assert h["price"] == pytest.approx(1100.0)
    assert h["value"] == pytest.approx(110_000.0)
    assert h["cost_basis"] == pytest.approx(100_000.0)
    assert h["pnl"] == pytest.approx(10_000.0)
