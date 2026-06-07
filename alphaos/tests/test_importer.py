"""Unit tests for the Avanza CSV importer in its LEDGER form.

The importer now feeds the transaction ledger: `parse_avanza_csv` yields raw
buy/sell rows (holdings are derived later), `import_transactions` persists those
rows by replacing the file's date range and recomputes derived holdings, and
`preview_import` shows what would happen without ever touching the DB.

Everything runs against a fresh in-memory SQLite DB and a small INLINE CSV
string (never the real export file), so the suite is fast and deterministic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import (
    Base,
    CashFlow,
    CashFlowKind,
    Holding,
    Transaction,
    TransactionKind,
    TxnSource,
)
from alphaos.db.allocation import (
    get_holding_by_isin,
    list_holdings,
    upsert_holding,
    upsert_sleeve,
)
from alphaos.db.cash_flows import add_cash_flow, list_cash_flows
from alphaos.db.importer import import_transactions, parse_avanza_csv, preview_import


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


def _count(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)))


# --------------------------------------------------------------------------- #
# parse_avanza_csv (pure: transaction rows, deposits, date range)
# --------------------------------------------------------------------------- #

def test_parse_returns_buy_sell_transaction_rows():
    res = parse_avanza_csv(_csv())

    # parse no longer derives holdings; it yields raw ledger rows.
    assert "holdings" not in res
    txns = res["transactions"]
    # 3 buys + 2 sells == 5 rows; the two Insättning rows are NOT transactions.
    assert len(txns) == 5

    kinds = sorted(t["kind"] for t in txns)
    assert kinds == ["buy", "buy", "buy", "sell", "sell"]

    by_isin: dict[str, list] = {}
    for t in txns:
        by_isin.setdefault(t["isin"], []).append(t)
    assert set(by_isin) == {"SE0000000001", "SE0000000002"}

    # ISIN1: two buys (100@50, 100@70) and one sell (50@80), each a Decimal.
    isin1 = by_isin["SE0000000001"]
    assert len(isin1) == 3
    buys = sorted((t for t in isin1 if t["kind"] == "buy"), key=lambda t: t["price"])
    assert [(_dec(b["quantity"]), _dec(b["price"])) for b in buys] == [
        (Decimal("100"), Decimal("50")),
        (Decimal("100"), Decimal("70")),
    ]
    sell = next(t for t in isin1 if t["kind"] == "sell")
    # Antal is stored as a positive magnitude even though the CSV is "-50".
    assert _dec(sell["quantity"]) == Decimal("50")
    assert sell["name"] == "Alpha AB"
    assert sell["currency"] == "SEK"


def test_parse_deposits_and_date_range():
    res = parse_avanza_csv(_csv())
    assert _dec(res["deposits_total"]) == Decimal("15000")
    assert res["date_min"] == "2026-01-05"
    assert res["date_max"] == "2026-05-01"
    # rows == every data line in the file (deposits included).
    assert res["rows"] == 7


# --------------------------------------------------------------------------- #
# import_transactions (DB) — persists ledger, derives holdings
# --------------------------------------------------------------------------- #

def test_import_persists_transactions_and_derives_holdings(session):
    res = import_transactions(session, _csv())

    # All 5 buy/sell rows landed in the ledger.
    assert res["transactions_imported"] == 5
    assert _count(session, Transaction) == 5
    # Only ISIN1 is still open -> exactly one derived holding.
    assert res["holdings_count"] == 1
    assert res["deposits_total"] == pytest.approx(15000.0)
    assert res["date_min"] == "2026-01-05"
    assert res["date_max"] == "2026-05-01"

    # Ledger rows are tagged as the avanza source.
    assert all(t.source is TxnSource.avanza for t in session.scalars(select(Transaction)))

    open_holdings = [h for h in list_holdings(session) if _dec(h.quantity) > 0]
    assert len(open_holdings) == 1
    h = get_holding_by_isin(session, "SE0000000001")
    assert h is not None
    assert _dec(h.quantity) == Decimal("150")
    assert _dec(h.avg_price) == Decimal("60")
    assert _dec(h.cost_basis_sek) == Decimal("9000")
    assert h.acquired_at.isoformat() == "2026-01-10"


def test_import_is_idempotent_replace_by_range(session):
    first = import_transactions(session, _csv())
    assert first["transactions_imported"] == 5

    txn_count_1 = _count(session, Transaction)
    h1 = get_holding_by_isin(session, "SE0000000001")
    snap_1 = (_dec(h1.quantity), _dec(h1.avg_price), _dec(h1.cost_basis_sek))
    assert txn_count_1 == 5

    # Re-import the SAME csv -> replace-by-range, so the ledger COUNT is unchanged.
    second = import_transactions(session, _csv())
    assert second["transactions_imported"] == 5
    assert second["holdings_count"] == 1
    assert _count(session, Transaction) == txn_count_1 == 5

    # Derived holdings are identical (no doubling of qty / cost).
    h2 = get_holding_by_isin(session, "SE0000000001")
    assert (_dec(h2.quantity), _dec(h2.avg_price), _dec(h2.cost_basis_sek)) == snap_1
    assert snap_1 == (Decimal("150"), Decimal("60"), Decimal("9000"))


def test_import_preserves_holding_metadata(session):
    # Pre-existing holding (matched by ISIN) carrying a sleeve + ticker.
    sleeve = upsert_sleeve(session, "RAW", name="RAW", target_weight=Decimal("0.45"))
    upsert_holding(
        session,
        sleeve_id=sleeve.id,
        symbol="ALPH",
        isin="SE0000000001",
        name="Alpha AB",
    )

    import_transactions(session, _csv())

    h = get_holding_by_isin(session, "SE0000000001")
    # qty/cost are derived from the ledger; sleeve + symbol metadata survive.
    assert h.sleeve_id == sleeve.id
    assert h.symbol == "ALPH"
    assert _dec(h.quantity) == Decimal("150")
    assert _dec(h.avg_price) == Decimal("60")
    # Matched, not duplicated.
    assert len(list_holdings(session)) == 1


# --------------------------------------------------------------------------- #
# preview_import (pure: summary + holdings, never writes)
# --------------------------------------------------------------------------- #

def test_preview_reports_summary_and_holdings_without_writing(session):
    before_txns = _count(session, Transaction)
    before_holdings = _count(session, Holding)

    preview = preview_import(_csv())

    summary = preview["summary"]
    assert summary["transactions"] == 5
    assert summary["holdings_count"] == 1
    assert summary["deposits_total"] == pytest.approx(15000.0)
    assert summary["date_min"] == "2026-01-05"
    assert summary["date_max"] == "2026-05-01"
    assert summary["rows"] == 7

    # Preview derives the same open position as the real import would.
    holdings = {h["isin"]: h for h in preview["holdings"]}
    assert set(holdings) == {"SE0000000001"}          # fully-sold ISIN2 excluded
    h1 = holdings["SE0000000001"]
    assert _dec(h1["quantity"]) == Decimal("150")
    assert _dec(h1["avg_price"]) == Decimal("60")
    assert _dec(h1["cost_basis_sek"]) == Decimal("9000")

    # The DB is untouched: row counts are exactly what they were before.
    assert _count(session, Transaction) == before_txns == 0
    assert _count(session, Holding) == before_holdings == 0


# --------------------------------------------------------------------------- #
# cash-flow routing: Insättning -> deposit, Uttag -> withdrawal
# --------------------------------------------------------------------------- #

# One deposit (+10000), one buy (untouched by the cash-flow routing), and one
# Uttag whose Belopp is already negative — the parser normalizes by sign anyway.
_CSV_CF = "\n".join([
    _HEADER,
    "2026-01-05;ISK;Insättning;;;;10000,00;SEK;0;1;SEK;;",
    "2026-02-01;ISK;Köp;Alpha AB;100;50,00;-5000,00;SEK;0;1;SEK;SE0000000001;",
    "2026-03-01;ISK;Uttag;;;;-2000,00;SEK;0;1;SEK;;",
])


def test_parse_routes_deposit_and_withdrawal_into_cash_flows():
    res = parse_avanza_csv(_CSV_CF)

    cfs = res["cash_flows"]
    assert len(cfs) == 2
    by_kind = {c["kind"]: c for c in cfs}
    # Insättning -> deposit, signed positive.
    assert _dec(by_kind["deposit"]["amount_sek"]) == Decimal("10000")
    # Uttag -> withdrawal, signed negative regardless of the CSV's sign.
    assert _dec(by_kind["withdrawal"]["amount_sek"]) == Decimal("-2000")
    # deposits_total stays back-compat (sums deposit magnitudes only).
    assert _dec(res["deposits_total"]) == Decimal("10000")
    # The single Köp row is still a transaction, not a cash flow.
    assert len(res["transactions"]) == 1


def test_import_persists_cash_flows_and_leaves_buys_intact(session):
    res = import_transactions(session, _CSV_CF)

    assert res["cash_flows_imported"] == 2
    # net flow = +10000 - 2000.
    assert res["cash_flows_net_sek"] == pytest.approx(8000.0)
    # The buy is unaffected by cash-flow routing.
    assert res["transactions_imported"] == 1
    assert _count(session, Transaction) == 1

    cfs = list_cash_flows(session)
    assert len(cfs) == 2
    assert all(c.source is TxnSource.avanza for c in cfs)
    dep = next(c for c in cfs if c.kind is CashFlowKind.deposit)
    wd = next(c for c in cfs if c.kind is CashFlowKind.withdrawal)
    assert _dec(dep.amount_sek) == Decimal("10000")
    assert _dec(wd.amount_sek) == Decimal("-2000")


def test_reimport_cash_flows_is_idempotent_and_preserves_manual(session):
    import_transactions(session, _CSV_CF)

    # A hand-entered cash flow inside the file's date range must survive.
    add_cash_flow(session, date="2026-02-10", amount_sek=500, kind="deposit", source="manual")

    # Re-import the SAME file -> avanza rows replaced by range, not appended.
    second = import_transactions(session, _CSV_CF)
    assert second["cash_flows_imported"] == 2
    # Buy/sell ledger stays a single row across the re-import.
    assert _count(session, Transaction) == 1

    cfs = list_cash_flows(session)
    avanza = [c for c in cfs if c.source is TxnSource.avanza]
    manual = [c for c in cfs if c.source is TxnSource.manual]
    assert len(avanza) == 2            # not doubled
    assert len(manual) == 1            # manual untouched
    assert _dec(manual[0].amount_sek) == Decimal("500")
    assert _count(session, CashFlow) == 3
