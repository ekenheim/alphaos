"""Sleeve + holdings services: CRUD and the target-vs-actual allocation view.

Weights are computed off total gross market value (sum of holding market values).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AssetClass, Holding, Sleeve, SleeveKind

_ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_kind(kind: SleeveKind | str | None) -> SleeveKind | None:
    if kind is None:
        return None
    return kind if isinstance(kind, SleeveKind) else SleeveKind(str(kind))


def _as_asset_class(ac: AssetClass | str | None) -> AssetClass:
    if ac is None:
        return AssetClass.equity
    return ac if isinstance(ac, AssetClass) else AssetClass(str(ac))


# --- Sleeves ---

def list_sleeves(session: Session) -> list[Sleeve]:
    return list(session.scalars(select(Sleeve).order_by(Sleeve.sort_order, Sleeve.code)))


def get_sleeve(session: Session, code: str) -> Sleeve | None:
    return session.scalars(select(Sleeve).where(Sleeve.code == code)).first()


def upsert_sleeve(
    session: Session,
    code: str,
    *,
    name: str | None = None,
    kind: SleeveKind | str | None = None,
    target_weight: Any = None,
    sort_order: int | None = None,
    notes: str | None = None,
) -> Sleeve:
    sleeve = get_sleeve(session, code)
    if sleeve is None:
        sleeve = Sleeve(code=code, name=name or code)
        session.add(sleeve)
    if name is not None:
        sleeve.name = name
    k = _as_kind(kind)
    if k is not None:
        sleeve.kind = k
    if target_weight is not None:
        sleeve.target_weight = _dec(target_weight)
    if sort_order is not None:
        sleeve.sort_order = sort_order
    if notes is not None:
        sleeve.notes = notes
    session.flush()
    return sleeve


_DEFAULT_SLEEVES = [
    ("CNDX", "iShares NASDAQ-100 UCITS (CNDX)", SleeveKind.beta_core, "0.24", 1,
     "Beta core. IE00B53SZB19."),
    ("VVSM", "VanEck Semiconductors UCITS (VVSM)", SleeveKind.tilt, "0.11", 2,
     "Growth tilt toward the highest-growth slice of the index."),
    ("RAW", "RAW K=20 discretionary equity", SleeveKind.discretionary_equity, "0.45", 3,
     "20 single US stocks ~2.25% each; monthly momentum reselection. Active-return engine."),
    ("CA", "Cross-asset insurance", SleeveKind.cross_asset_insurance, "0.10", 4,
     "~47.5% US Treasuries (IDTL 20yr, IE00B1FZS798 7-10yr) + ~52.5% commodities."),
    ("LOWVOL", "Low-beta carve (BAB)", SleeveKind.low_vol_carve, "0.10", 5,
     "Betting-against-beta exposure tilt, adopted 2026-06-06."),
]


def seed_default_sleeves(session: Session) -> int:
    """Seed/refresh the V2-FRONTIER sleeves + target weights. Idempotent."""
    n = 0
    for code, name, kind, tw, order, notes in _DEFAULT_SLEEVES:
        sleeve = get_sleeve(session, code)
        if sleeve is None:
            session.add(Sleeve(
                code=code, name=name, kind=kind,
                target_weight=_dec(tw), sort_order=order, notes=notes,
            ))
            n += 1
        else:
            sleeve.name = name
            sleeve.kind = kind
            sleeve.target_weight = _dec(tw)
            sleeve.sort_order = order
            if not sleeve.notes:
                sleeve.notes = notes
    session.flush()
    return n


# --- Holdings ---

def list_holdings(session: Session, sleeve_id: int | None = None) -> list[Holding]:
    stmt = select(Holding).order_by(Holding.sleeve_id, Holding.symbol)
    if sleeve_id is not None:
        stmt = stmt.where(Holding.sleeve_id == sleeve_id)
    return list(session.scalars(stmt))


def upsert_holding(
    session: Session,
    *,
    id: int | None = None,
    sleeve_code: str | None = None,
    sleeve_id: int | None = None,
    symbol: str,
    isin: str | None = None,
    name: str | None = None,
    asset_class: AssetClass | str | None = None,
    currency: str = "SEK",
    quantity: Any = 0,
    market_value: Any = 0,
    as_of: dt.date | str | None = None,
    notes: str | None = None,
) -> Holding:
    if sleeve_id is None and sleeve_code is not None:
        sleeve = get_sleeve(session, sleeve_code)
        if sleeve is None:
            raise ValueError(f"unknown sleeve '{sleeve_code}'")
        sleeve_id = sleeve.id

    holding = session.get(Holding, id) if id is not None else None
    if holding is None:
        holding = Holding(symbol=symbol.strip().upper())
        session.add(holding)
    else:
        holding.symbol = symbol.strip().upper()

    holding.sleeve_id = sleeve_id
    if isin is not None:
        holding.isin = isin.strip().upper() or None
    if name is not None:
        holding.name = name
    holding.asset_class = _as_asset_class(asset_class)
    holding.currency = currency or "SEK"
    holding.quantity = _dec(quantity)
    holding.market_value = _dec(market_value)
    if isinstance(as_of, str):
        as_of = dt.date.fromisoformat(as_of) if as_of else None
    holding.as_of = as_of
    if notes is not None:
        holding.notes = notes
    session.flush()
    return holding


def delete_holding(session: Session, holding_id: int) -> bool:
    holding = session.get(Holding, holding_id)
    if holding is None:
        return False
    session.delete(holding)
    session.flush()
    return True


def total_gross_value(session: Session) -> Decimal:
    """Sum of all holding market values (base currency)."""
    return sum((_dec(h.market_value) for h in list_holdings(session)), _ZERO)


def allocation(session: Session) -> dict[str, Any]:
    """Target vs current allocation per sleeve, with drift and rebalance deltas.

    rebalance_delta is target_value - current_value (positive => buy to reach target).
    """
    sleeves = list_sleeves(session)
    holdings = list_holdings(session)
    by_sleeve: dict[int | None, list[Holding]] = {}
    for h in holdings:
        by_sleeve.setdefault(h.sleeve_id, []).append(h)

    total = sum((_dec(h.market_value) for h in holdings), _ZERO)

    rows = []
    for s in sleeves:
        members = by_sleeve.get(s.id, [])
        cur_val = sum((_dec(h.market_value) for h in members), _ZERO)
        cur_w = (cur_val / total) if total > _ZERO else _ZERO
        tgt_w = _dec(s.target_weight)
        tgt_val = tgt_w * total
        rows.append({
            "id": s.id,
            "code": s.code,
            "name": s.name,
            "kind": s.kind.value,
            "target_weight": float(tgt_w),
            "current_value": float(cur_val),
            "current_weight": float(cur_w),
            "drift": float(cur_w - tgt_w),
            "rebalance_delta": float(tgt_val - cur_val),
            "holdings": [
                {
                    "id": h.id,
                    "symbol": h.symbol,
                    "name": h.name,
                    "isin": h.isin,
                    "asset_class": h.asset_class.value,
                    "market_value": float(_dec(h.market_value)),
                    "weight": float(_dec(h.market_value) / total) if total > _ZERO else 0.0,
                }
                for h in members
            ],
        })

    unassigned = by_sleeve.get(None, [])
    unassigned_val = sum((_dec(h.market_value) for h in unassigned), _ZERO)

    return {
        "total_gross_value": float(total),
        "target_weight_sum": float(sum((_dec(s.target_weight) for s in sleeves), _ZERO)),
        "sleeves": rows,
        "unassigned": {
            "current_value": float(unassigned_val),
            "current_weight": float(unassigned_val / total) if total > _ZERO else 0.0,
            "holdings": [
                {"id": h.id, "symbol": h.symbol, "market_value": float(_dec(h.market_value))}
                for h in unassigned
            ],
        },
    }
