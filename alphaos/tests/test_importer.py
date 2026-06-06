"""Unit tests for the Avanza CSV importer (parse + cost-basis reconstruction).

Everything runs against a fresh in-memory SQLite DB and a small INLINE CSV
string (never the real export file), so the suite is fast and deterministic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import Base
from alphaos.db.allocation import (
    get_holding_by_isin,
    list_holdings,
    upsert_holding,
    upsert_sleeve,
)
from alphaos.db.importer import import_transactions, parse_avanza_csv


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


_HEADER = (
    "Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;"
    "Transaktionsvaluta;Courtage;Valutakurs;Instrumentvaluta;ISIN;Resultat"
)

# ISIN1 (SE0000000001): buy 100@50, buy 100@70 -> 200 @ avg 60, then sell 50.
#   net qty 150, avg_price stays 60, cost_basis 12000 -> 9000 (proportional).
# ISIN2 (SE0000000002): buy 10@100 then sell all 10 -> fully closed, excluded.
# Two Insättning rows -> deposits_total 15000.
# Rows are intentionally out of date order to exercise the chronological sort.
_CSV = "\n".join([
    _HEADER,
    "2026-05-01;ISK;Insättning;;;;5000,00;SEK;0;1;SEK;;",
    "2026-04-10;ISK;Sälj;Beta AB;-10;110,00;1100,00;SEK;0;1;SEK;SE0000000002;100,00",
    "2026-03-20;ISK;Sälj;Alpha AB;-50;80,00;4000,00;SEK;0;1;SEK;SE0000000001;1000,00",
    "2026-03-01;ISK;Köp;Beta AB;10;100,00;-1000,00;SEK;0;1;SEK;SE0000000002;",
    "2026-02-15;ISK;Köp;Alpha AB;100;70,00;-7000,00;SEK;0;1;SEK;SE0000000001;",
    "2026-01-10;ISK;Köp;Alpha AB;100;50,00;-5000,00;SEK;0;1;SEK;SE0000000001;",
    "2026-01-05;ISK;Insättning;;;;10000,00;SEK;0;1;SEK;;",
])


def _csv() -> str:
    return _CSV


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


# --------------------------------------------------------------------------- #
# parse_avanza_csv (pure)
# --------------------------------------------------------------------------- #

def test_parse_buy_then_partial_sell_cost_basis():
    res = parse_avanza_csv(_csv())
    holdings = {h["isin"]: h for h in res["holdings"]}

    # ISIN1 survives with the partial position.
    h1 = holdings["SE0000000001"]
    assert _dec(h1["quantity"]) == Decimal("150")
    # avg purchase price is the weighted avg of the two BUYS (60), untouched by the sell.
    assert _dec(h1["avg_price"]) == Decimal("60")
    # cost basis reduced proportionally: 12000 * (150/200) = 9000.
    assert _dec(h1["cost_basis_sek"]) == Decimal("9000")
    assert h1["currency"] == "SEK"
    assert h1["name"] == "Alpha AB"


def test_parse_fully_sold_isin_excluded():
    res = parse_avanza_csv(_csv())
    isins = {h["isin"] for h in res["holdings"]}
    # ISIN2 was bought then fully sold -> not a current holding.
    assert "SE0000000002" not in isins
    assert isins == {"SE0000000001"}


def test_parse_deposits_and_date_range():
    res = parse_avanza_csv(_csv())
    assert _dec(res["deposits_total"]) == Decimal("15000")
    assert res["date_min"] == "2026-01-05"
    assert res["date_max"] == "2026-05-01"
    assert res["rows"] == 7


# --------------------------------------------------------------------------- #
# import_transactions (DB) + idempotency
# --------------------------------------------------------------------------- #

def test_import_creates_holdings(session):
    res = import_transactions(session, _csv())
    assert res["created"] == 1
    assert res["updated"] == 0
    assert res["holdings_count"] == 1
    assert res["deposits_total"] == pytest.approx(15000.0)
    assert res["date_min"] == "2026-01-05"
    assert res["date_max"] == "2026-05-01"

    # Only the surviving ISIN landed in the DB.
    rows = list_holdings(session)
    assert len(rows) == 1
    h = get_holding_by_isin(session, "SE0000000001")
    assert h is not None
    assert _dec(h.quantity) == Decimal("150")
    assert _dec(h.avg_price) == Decimal("60")
    assert _dec(h.cost_basis_sek) == Decimal("9000")


def test_import_is_idempotent(session):
    first = import_transactions(session, _csv())
    assert first["created"] == 1

    h_after_first = get_holding_by_isin(session, "SE0000000001")
    qty1 = _dec(h_after_first.quantity)
    avg1 = _dec(h_after_first.avg_price)
    cost1 = _dec(h_after_first.cost_basis_sek)

    # Re-import the SAME csv -> nothing new, no doubling.
    second = import_transactions(session, _csv())
    assert second["created"] == 0
    assert second["updated"] == 1
    assert second["holdings_count"] == 1

    rows = list_holdings(session)
    assert len(rows) == 1

    h_after_second = get_holding_by_isin(session, "SE0000000001")
    assert _dec(h_after_second.quantity) == qty1 == Decimal("150")
    assert _dec(h_after_second.avg_price) == avg1 == Decimal("60")
    assert _dec(h_after_second.cost_basis_sek) == cost1 == Decimal("9000")


def test_import_preserves_existing_sleeve_and_symbol(session):
    # Pre-existing holding matched by ISIN, with a sleeve + ticker already assigned.
    sleeve = upsert_sleeve(session, "RAW", name="RAW", target_weight=Decimal("0.45"))
    upsert_holding(
        session,
        sleeve_id=sleeve.id,
        symbol="ALPH",
        isin="SE0000000001",
        name="Alpha AB",
        currency="SEK",
        quantity=Decimal("1"),
    )

    import_transactions(session, _csv())

    h = get_holding_by_isin(session, "SE0000000001")
    # Import overwrote qty/cost from the file history but kept sleeve + symbol.
    assert h.sleeve_id == sleeve.id
    assert h.symbol == "ALPH"
    assert _dec(h.quantity) == Decimal("150")
    assert _dec(h.avg_price) == Decimal("60")
    # Still a single holding (matched, not duplicated).
    assert len(list_holdings(session)) == 1
