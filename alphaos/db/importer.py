"""Import an Avanza 'transaktioner' CSV into the transaction ledger.

The export is semicolon-delimited, UTF-8 BOM, comma decimals. Columns:
  Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;
  Transaktionsvaluta;Courtage;Valutakurs;Instrumentvaluta;ISIN;Resultat

Each Köp/Sälj row becomes a `transactions` row (source='avanza'); holdings are then
recomputed (derived) from the ledger. Re-importing replaces the file's date range
(see transactions.replace_avanza_range), so it is idempotent. Insättning/Uttag rows
populate the cash-flow ledger (deposit +, withdrawal -) via
cash_flows.replace_avanza_cashflows; deposits_total is still reported for back-compat.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from . import cash_flows as dbcf
from . import transactions as txns
from .allocation import list_holdings

_BUY = {"köp", "kop", "buy"}
_SELL = {"sälj", "salj", "sell"}
_DEPOSIT = {"insättning", "insattning", "deposit"}
_WITHDRAW = {"uttag", "withdrawal"}


def _num(s: str | None) -> Decimal | None:
    if s is None:
        return None
    s = s.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _col(row: dict, *names: str) -> str | None:
    for n in names:
        for k, v in row.items():
            if k and k.strip().lower() == n.lower():
                return v
    return None


def parse_avanza_csv(content: bytes | str) -> dict[str, Any]:
    """Parse into transaction rows. Returns {transactions, cash_flows,
    deposits_total, date_min, date_max, rows}. Pure (no DB) — safe for preview."""
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))

    out: list[dict[str, Any]] = []
    cash_flows: list[dict[str, Any]] = []
    deposits = Decimal("0")
    dmin: str | None = None
    dmax: str | None = None

    for r in rows:
        when = _col(r, "Datum")
        if when:
            dmin = when if dmin is None else min(dmin, when)
            dmax = when if dmax is None else max(dmax, when)
        ttype = (_col(r, "Typ av transaktion") or "").strip().lower()

        if ttype in _DEPOSIT:
            amt = _num(_col(r, "Belopp"))
            if amt and when:
                deposits += abs(amt)
                cash_flows.append({
                    "date": when,
                    "amount_sek": abs(amt),
                    "kind": "deposit",
                    "note": (_col(r, "Värdepapper/beskrivning") or "").strip() or None,
                })
            continue
        if ttype in _WITHDRAW:
            amt = _num(_col(r, "Belopp"))
            if amt and when:
                cash_flows.append({
                    "date": when,
                    "amount_sek": -abs(amt),
                    "kind": "withdrawal",
                    "note": (_col(r, "Värdepapper/beskrivning") or "").strip() or None,
                })
            continue
        if ttype in _BUY:
            kind = "buy"
        elif ttype in _SELL:
            kind = "sell"
        else:
            continue

        isin = (_col(r, "ISIN") or "").strip().upper()
        antal = _num(_col(r, "Antal"))
        kurs = _num(_col(r, "Kurs"))
        if not isin or antal is None or kurs is None or when is None:
            continue

        out.append({
            "date": when,
            "isin": isin,
            "name": (_col(r, "Värdepapper/beskrivning", "Värdepapper") or "").strip() or None,
            "currency": (_col(r, "Instrumentvaluta") or "").strip().upper() or "SEK",
            "kind": kind,
            "quantity": abs(antal),
            "price": kurs,
            "amount_sek": _num(_col(r, "Belopp")),
            "fees_sek": _num(_col(r, "Courtage")) or Decimal("0"),
            "fx_rate": _num(_col(r, "Valutakurs")),
        })

    return {
        "transactions": out,
        "cash_flows": cash_flows,
        "deposits_total": deposits,
        "date_min": dmin,
        "date_max": dmax,
        "rows": len(rows),
    }


def preview_import(content: bytes | str) -> dict[str, Any]:
    """Parse + aggregate WITHOUT writing — shows what the import would produce."""
    res = parse_avanza_csv(content)
    agg = txns.aggregate(res["transactions"])
    holdings = [
        {
            "isin": isin,
            "name": a["name"],
            "currency": a["currency"],
            "quantity": float(a["qty"]),
            "avg_price": float(a["avg_price"]),
            "cost_basis_sek": float(a["cost_sek"]),
            "acquired_at": a["acquired_at"].isoformat() if a["acquired_at"] else None,
        }
        for isin, a in agg.items()
    ]
    return {
        "summary": {
            "transactions": len(res["transactions"]),
            "holdings_count": len(holdings),
            "deposits_total": float(res["deposits_total"]),
            "cash_flows_count": len(res["cash_flows"]),
            "cash_flows_net_sek": float(
                sum((Decimal(str(f["amount_sek"])) for f in res["cash_flows"]), Decimal("0"))
            ),
            "date_min": res["date_min"],
            "date_max": res["date_max"],
            "rows": res["rows"],
        },
        "holdings": holdings,
    }


def import_transactions(session: Session, content: bytes | str) -> dict[str, Any]:
    """Persist the CSV's buy/sell rows (replacing the file's date range) and
    recompute derived holdings. Idempotent for full-history exports."""
    res = parse_avanza_csv(content)
    imported = 0
    if res["transactions"] and res["date_min"] and res["date_max"]:
        imported = txns.replace_avanza_range(
            session, res["transactions"], res["date_min"], res["date_max"]
        )
    txns.recompute_holdings(session)
    open_holdings = sum(1 for h in list_holdings(session) if (h.quantity or 0) > 0)

    cf_imported = 0
    cf_net = Decimal("0")
    if res["cash_flows"] and res["date_min"] and res["date_max"]:
        cf_imported = dbcf.replace_avanza_cashflows(
            session, res["cash_flows"], res["date_min"], res["date_max"]
        )
        cf_net = sum((Decimal(str(f["amount_sek"])) for f in res["cash_flows"]), Decimal("0"))

    return {
        "transactions_imported": imported,
        "holdings_count": open_holdings,
        "deposits_total": float(res["deposits_total"]),
        "cash_flows_imported": cf_imported,
        "cash_flows_net_sek": float(cf_net),
        "date_min": res["date_min"],
        "date_max": res["date_max"],
        "rows": res["rows"],
    }
