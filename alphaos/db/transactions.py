"""Transaction ledger services — the source of truth for positions.

Every buy/sell is a row in `transactions`; current holdings (qty, avg_price,
cost_basis_sek, acquired_at) are DERIVED by aggregating them with the
average-cost method. The same aggregator powers CSV-import preview, holdings
recompute, and per-position history.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .allocation import get_holding_by_isin
from .models import Holding, Transaction, TransactionKind, TxnSource

_ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_kind(k: TransactionKind | str) -> TransactionKind:
    return k if isinstance(k, TransactionKind) else TransactionKind(str(k).lower())


def _as_source(s: TxnSource | str) -> TxnSource:
    return s if isinstance(s, TxnSource) else TxnSource(str(s).lower())


def _as_date(d: dt.date | str) -> dt.date:
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))


# --- Aggregation (pure: works on normalized dicts) ---

def _norm(t: Any) -> dict[str, Any]:
    """Normalize a Transaction ORM row OR a parsed dict to a common shape."""
    if isinstance(t, Transaction):
        return {
            "date": t.date, "isin": t.isin, "name": t.name, "currency": t.currency,
            "kind": t.kind.value, "quantity": _dec(t.quantity), "price": _dec(t.price),
            "amount_sek": None if t.amount_sek is None else _dec(t.amount_sek),
            "id": t.id,
        }
    return {
        "date": _as_date(t["date"]) if t.get("date") else None,
        "isin": (t.get("isin") or "").strip().upper(),
        "name": t.get("name"),
        "currency": (t.get("currency") or "SEK").upper(),
        "kind": str(t.get("kind")).lower(),
        "quantity": _dec(t.get("quantity") or 0),
        "price": _dec(t.get("price") or 0),
        "amount_sek": None if t.get("amount_sek") is None else _dec(t["amount_sek"]),
        "id": t.get("id", 0),
    }


def aggregate(txns: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Average-cost aggregation per ISIN. Returns only OPEN positions (net qty > 0).

    Each value: {qty, avg_price, cost_sek, acquired_at, name, currency} as Decimals.
    """
    rows = [_norm(t) for t in txns]
    rows.sort(key=lambda r: (r["date"] or dt.date.min, r["id"]))
    acc: dict[str, dict[str, Any]] = {}
    for r in rows:
        isin = r["isin"]
        if not isin:
            continue
        h = acc.setdefault(isin, {
            "qty": _ZERO, "cost_ccy": _ZERO, "cost_sek": _ZERO,
            "acquired_at": None, "name": r["name"], "currency": r["currency"],
        })
        if r["name"] and not h["name"]:
            h["name"] = r["name"]
        h["currency"] = r["currency"] or h["currency"]
        q = r["quantity"]
        if r["kind"] == "buy":
            if r["date"] and (h["acquired_at"] is None or r["date"] < h["acquired_at"]):
                h["acquired_at"] = r["date"]
            h["qty"] += q
            h["cost_ccy"] += q * r["price"]
            h["cost_sek"] += abs(r["amount_sek"]) if r["amount_sek"] is not None else (q * r["price"])
        else:  # sell: reduce at average cost
            if h["qty"] > 0:
                avg_ccy = h["cost_ccy"] / h["qty"]
                avg_sek = h["cost_sek"] / h["qty"]
                reduce = min(q, h["qty"])
                h["cost_ccy"] -= avg_ccy * reduce
                h["cost_sek"] -= avg_sek * reduce
                h["qty"] -= reduce
            else:
                h["qty"] -= q

    out: dict[str, dict[str, Any]] = {}
    for isin, h in acc.items():
        if h["qty"] <= _ZERO:
            continue
        out[isin] = {
            "qty": h["qty"],
            "avg_price": h["cost_ccy"] / h["qty"],
            "cost_sek": h["cost_sek"],
            "acquired_at": h["acquired_at"],
            "name": h["name"],
            "currency": h["currency"],
        }
    return out


# --- Queries ---

def list_transactions(
    session: Session, isin: str | None = None, limit: int | None = None
) -> list[Transaction]:
    stmt = select(Transaction).order_by(Transaction.date, Transaction.id)
    if isin:
        stmt = stmt.where(Transaction.isin == isin.strip().upper())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def position_history(session: Session, isin: str) -> list[dict[str, Any]]:
    """Chronological transactions for one ISIN with the running quantity."""
    rows: list[dict[str, Any]] = []
    run = _ZERO
    for t in list_transactions(session, isin=isin):
        delta = _dec(t.quantity) if t.kind is TransactionKind.buy else -_dec(t.quantity)
        run += delta
        rows.append({
            "id": t.id,
            "date": t.date.isoformat() if t.date else None,
            "kind": t.kind.value,
            "quantity": float(_dec(t.quantity)),
            "price": float(_dec(t.price)),
            "currency": t.currency,
            "amount_sek": float(_dec(t.amount_sek)) if t.amount_sek is not None else None,
            "fees_sek": float(_dec(t.fees_sek)),
            "running_qty": float(run),
            "source": t.source.value,
            "note": t.note,
        })
    return rows


# --- Mutations (each recomputes affected holdings) ---

def recompute_holdings(session: Session, isins: Iterable[str] | None = None) -> None:
    """Rebuild derived holding fields (qty/avg_price/cost_basis_sek/acquired_at)
    from the transaction ledger, preserving metadata (sleeve, symbol, prices)."""
    all_txns = list_transactions(session)
    agg = aggregate(all_txns)

    scope = {i.strip().upper() for i in isins} if isins is not None else None

    for isin, a in agg.items():
        if scope is not None and isin not in scope:
            continue
        h = get_holding_by_isin(session, isin)
        if h is None:
            h = Holding(symbol="", isin=isin)
            session.add(h)
        if not h.name and a["name"]:
            h.name = a["name"]
        h.currency = a["currency"]
        h.quantity = a["qty"]
        h.avg_price = a["avg_price"]
        h.cost_basis_sek = a["cost_sek"]
        h.acquired_at = a["acquired_at"]

    # Zero out holdings that no longer have an open position.
    holdings = session.scalars(select(Holding)).all()
    for h in holdings:
        key = (h.isin or "").upper()
        if not key or key in agg:
            continue
        if scope is not None and key not in scope:
            continue
        h.quantity = _ZERO
        h.cost_basis_sek = _ZERO
    session.flush()


def add_transaction(
    session: Session,
    *,
    date: dt.date | str,
    isin: str,
    kind: TransactionKind | str,
    quantity: Any,
    price: Any,
    currency: str = "SEK",
    amount_sek: Any = None,
    fees_sek: Any = 0,
    fx_rate: Any = None,
    symbol: str | None = None,
    name: str | None = None,
    source: TxnSource | str = TxnSource.manual,
    note: str | None = None,
) -> Transaction:
    isin = (isin or "").strip().upper()
    if not isin:
        raise ValueError("isin is required")
    qty = abs(_dec(quantity))
    if qty <= _ZERO:
        raise ValueError("quantity must be > 0")
    txn = Transaction(
        date=_as_date(date),
        isin=isin,
        symbol=(symbol or None),
        name=name,
        currency=(currency or "SEK").upper(),
        kind=_as_kind(kind),
        quantity=qty,
        price=_dec(price),
        amount_sek=_dec(amount_sek) if amount_sek is not None else None,
        fees_sek=_dec(fees_sek or 0),
        fx_rate=_dec(fx_rate) if fx_rate is not None else None,
        source=_as_source(source),
        note=note,
    )
    session.add(txn)
    session.flush()
    recompute_holdings(session, isins=[isin])
    return txn


def delete_transaction(session: Session, txn_id: int) -> bool:
    txn = session.get(Transaction, txn_id)
    if txn is None:
        return False
    isin = txn.isin
    session.delete(txn)
    session.flush()
    recompute_holdings(session, isins=[isin])
    return True


def update_transaction(
    session: Session,
    txn_id: int,
    *,
    date: dt.date | str | None = None,
    isin: str | None = None,
    kind: TransactionKind | str | None = None,
    quantity: Any = None,
    price: Any = None,
    currency: str | None = None,
    amount_sek: Any = None,
    fees_sek: Any = None,
    fx_rate: Any = None,
    symbol: str | None = None,
    name: str | None = None,
    note: str | None = None,
) -> Transaction | None:
    """Edit an existing transaction in place; recompute the affected holding(s).

    Only provided fields are changed (partial update). Recomputes both the old and
    new ISIN when the ISIN changes. Returns the updated row, or None if not found.
    """
    txn = session.get(Transaction, txn_id)
    if txn is None:
        return None
    affected = {txn.isin}

    if date is not None:
        txn.date = _as_date(date)
    if isin is not None:
        txn.isin = (isin or "").strip().upper()
        if not txn.isin:
            raise ValueError("isin is required")
        affected.add(txn.isin)
    if kind is not None:
        txn.kind = _as_kind(kind)
    if quantity is not None:
        q = abs(_dec(quantity))
        if q <= _ZERO:
            raise ValueError("quantity must be > 0")
        txn.quantity = q
    if price is not None:
        txn.price = _dec(price)
    if currency is not None:
        txn.currency = (currency or "SEK").upper()
    if amount_sek is not None:
        txn.amount_sek = _dec(amount_sek)
    if fees_sek is not None:
        txn.fees_sek = _dec(fees_sek or 0)
    if fx_rate is not None:
        txn.fx_rate = _dec(fx_rate)
    if symbol is not None:
        txn.symbol = symbol or None
    if name is not None:
        txn.name = name
    if note is not None:
        txn.note = note

    session.flush()
    recompute_holdings(session, isins=affected)
    return txn


def replace_avanza_range(
    session: Session, txns: list[dict[str, Any]], date_min: dt.date | str, date_max: dt.date | str
) -> int:
    """Replace all source='avanza' transactions in [date_min, date_max] with `txns`.

    Makes re-importing a full Avanza export idempotent (and reflects corrections).
    Manual transactions are untouched.
    """
    dmin, dmax = _as_date(date_min), _as_date(date_max)
    session.execute(
        delete(Transaction).where(
            Transaction.source == TxnSource.avanza,
            Transaction.date >= dmin,
            Transaction.date <= dmax,
        )
    )
    for t in txns:
        session.add(Transaction(
            date=_as_date(t["date"]),
            isin=(t["isin"] or "").strip().upper(),
            symbol=t.get("symbol"),
            name=t.get("name"),
            currency=(t.get("currency") or "SEK").upper(),
            kind=_as_kind(t["kind"]),
            quantity=abs(_dec(t["quantity"])),
            price=_dec(t["price"]),
            amount_sek=_dec(t["amount_sek"]) if t.get("amount_sek") is not None else None,
            fees_sek=_dec(t.get("fees_sek") or 0),
            fx_rate=_dec(t["fx_rate"]) if t.get("fx_rate") is not None else None,
            source=TxnSource.avanza,
            note=t.get("note"),
        ))
    session.flush()
    return len(txns)
