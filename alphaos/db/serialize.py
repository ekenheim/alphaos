"""JSON serialization helpers for the V2-FRONTIER ORM objects.

Decimal -> float, datetime/date -> isoformat, enum -> .value.
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any

from .models import Holding, NavSnapshot, PortfolioConfig, Sleeve


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


def jsonable(value: Any) -> Any:
    """Recursively coerce Decimal/enum/datetime/date into JSON-native types."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def sleeve_to_dict(s: Sleeve) -> dict[str, Any]:
    return {
        "id": s.id,
        "code": s.code,
        "name": s.name,
        "kind": s.kind.value,
        "target_weight": _f(s.target_weight),
        "sort_order": s.sort_order,
        "notes": s.notes,
    }


def holding_to_dict(h: Holding) -> dict[str, Any]:
    return {
        "id": h.id,
        "sleeve_id": h.sleeve_id,
        "sleeve_code": h.sleeve.code if ("sleeve" in h.__dict__ and h.sleeve) else None,
        "symbol": h.symbol,
        "isin": h.isin,
        "name": h.name,
        "asset_class": h.asset_class.value,
        "currency": h.currency,
        "quantity": _f(h.quantity),
        "avg_price": _f(h.avg_price),
        "cost_basis_sek": _f(h.cost_basis_sek),
        "last_price": _f(h.last_price),
        "last_price_date": h.last_price_date.isoformat() if h.last_price_date else None,
        "price_source": h.price_source.value if h.price_source else None,
        "as_of": h.as_of.isoformat() if h.as_of else None,
        "notes": h.notes,
    }


def nav_snapshot_to_dict(n: NavSnapshot) -> dict[str, Any]:
    return {
        "id": n.id,
        "as_of": n.as_of.isoformat() if n.as_of else None,
        "gross_asset_value": _f(n.gross_asset_value),
        "loan_balance": _f(n.loan_balance),
        "net_contribution": _f(n.net_contribution),
        "equity": _f(n.equity),
        "twr_period": _f(n.twr_period),
        "nav_index": _f(n.nav_index),
        "peak_nav_index": _f(n.peak_nav_index),
        "drawdown": _f(n.drawdown),
        "effective_leverage": _f(n.effective_leverage),
        "belaningsgrad": _f(n.belaningsgrad),
        "delever_status": n.delever_status.value,
        "notes": n.notes,
    }


def config_to_dict(c: PortfolioConfig) -> dict[str, Any]:
    return {
        "base_currency": c.base_currency,
        "account_label": c.account_label,
        "leverage_target": _f(c.leverage_target),
        "leverage_floor": _f(c.leverage_floor),
        "glide_low_assets": _f(c.glide_low_assets),
        "glide_high_assets": _f(c.glide_high_assets),
        "blended_rate": _f(c.blended_rate),
        "repriced_rate": _f(c.repriced_rate),
        "belaningsgrad_cliff": _f(c.belaningsgrad_cliff),
        "delever_half_dd": _f(c.delever_half_dd),
        "delever_full_dd": _f(c.delever_full_dd),
        "reentry_recovery": _f(c.reentry_recovery),
        "forced_sale_dd": _f(c.forced_sale_dd),
        "external_reserve": _f(c.external_reserve),
        "planning_cagr_low": _f(c.planning_cagr_low),
        "planning_cagr_high": _f(c.planning_cagr_high),
        "fx_usd_sek": _f(c.fx_usd_sek),
        "fx_eur_sek": _f(c.fx_eur_sek),
        "fx_as_of": c.fx_as_of.isoformat() if c.fx_as_of else None,
        "fx_source": c.fx_source,
        "notes": c.notes,
    }
