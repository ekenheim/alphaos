"""Import an Avanza 'transaktioner' CSV into holdings (cost-basis reconstruction).

The export is semicolon-delimited, UTF-8 BOM, with comma decimals. Columns:
  Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;
  Transaktionsvaluta;Courtage;Valutakurs;Instrumentvaluta;ISIN;Resultat

Köp/Sälj rows are aggregated per ISIN (chronologically, average-cost method) into a
net quantity + average purchase price (instrument currency) + exact SEK cost (from
Belopp). Insättning rows are summed as total deposits (reported, not auto-applied).
Imported holdings land unassigned (no sleeve) for you to assign; the US ticker
(symbol) is left blank for you to set where you want MinIO pricing.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from .allocation import get_holding_by_isin, upsert_holding

_BUY = {"köp", "kop", "buy"}
_SELL = {"sälj", "salj", "sell"}
_DEPOSIT = {"insättning", "insattning", "deposit"}


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
    """Parse + aggregate. Returns {holdings:[...], deposits_total, date_min, date_max, rows}."""
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    # Process oldest-first so average cost builds correctly (file is newest-first).
    rows.sort(key=lambda r: (_col(r, "Datum") or ""))

    acc: dict[str, dict[str, Any]] = {}
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
            if amt:
                deposits += amt
            continue
        if ttype not in _BUY and ttype not in _SELL:
            continue

        isin = (_col(r, "ISIN") or "").strip().upper()
        antal = _num(_col(r, "Antal"))
        kurs = _num(_col(r, "Kurs"))
        belopp = _num(_col(r, "Belopp"))
        ccy = (_col(r, "Instrumentvaluta") or "").strip().upper() or "SEK"
        name = (_col(r, "Värdepapper/beskrivning", "Värdepapper") or "").strip()
        if not isin or antal is None or kurs is None:
            continue

        h = acc.setdefault(isin, {
            "isin": isin, "name": name, "currency": ccy,
            "qty": Decimal("0"), "cost_ccy": Decimal("0"), "cost_sek": Decimal("0"),
            "first_buy": None,
        })
        if name and not h["name"]:
            h["name"] = name

        if antal > 0:  # buy: add to position + cost basis
            if when and (h["first_buy"] is None or when < h["first_buy"]):
                h["first_buy"] = when
            h["qty"] += antal
            h["cost_ccy"] += antal * kurs
            h["cost_sek"] += abs(belopp) if belopp is not None else (antal * kurs)
        else:          # sell: reduce at average cost (avg price unchanged)
            sell_qty = -antal
            if h["qty"] > 0:
                avg_ccy = h["cost_ccy"] / h["qty"]
                avg_sek = h["cost_sek"] / h["qty"]
                reduce = min(sell_qty, h["qty"])
                h["cost_ccy"] -= avg_ccy * reduce
                h["cost_sek"] -= avg_sek * reduce
                h["qty"] -= reduce
            else:
                h["qty"] -= sell_qty  # selling with no recorded buys (pre-window) -> negative

    holdings: list[dict[str, Any]] = []
    for isin, h in acc.items():
        qty = h["qty"]
        if qty <= 0:  # fully closed (or pre-window) -> no current holding
            continue
        holdings.append({
            "isin": isin,
            "name": h["name"],
            "currency": h["currency"],
            "quantity": qty,
            "avg_price": (h["cost_ccy"] / qty),
            "cost_basis_sek": h["cost_sek"],
            "acquired_at": h["first_buy"],
        })

    return {
        "holdings": holdings,
        "deposits_total": deposits,
        "date_min": dmin,
        "date_max": dmax,
        "rows": len(rows),
    }


def import_transactions(session: Session, content: bytes | str) -> dict[str, Any]:
    """Parse the CSV and upsert holdings (matched by ISIN). Sleeve + symbol are
    preserved on existing holdings and left blank on new ones for you to set."""
    res = parse_avanza_csv(content)
    created = 0
    updated = 0
    for hd in res["holdings"]:
        existing = get_holding_by_isin(session, hd["isin"])
        upsert_holding(
            session,
            id=(existing.id if existing else None),
            sleeve_id=(existing.sleeve_id if existing else None),
            symbol=(existing.symbol if (existing and existing.symbol) else ""),
            isin=hd["isin"],
            name=hd["name"],
            currency=hd["currency"],
            quantity=hd["quantity"],
            avg_price=hd["avg_price"],
            cost_basis_sek=hd["cost_basis_sek"],
            acquired_at=hd.get("acquired_at"),
        )
        if existing:
            updated += 1
        else:
            created += 1

    return {
        "created": created,
        "updated": updated,
        "holdings_count": len(res["holdings"]),
        "deposits_total": float(res["deposits_total"]),
        "date_min": res["date_min"],
        "date_max": res["date_max"],
        "rows": res["rows"],
    }
