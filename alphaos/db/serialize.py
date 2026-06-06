"""JSON serialization helpers for DB models.

Decimal -> float, datetime -> isoformat(), enum -> .value. These produce the
plain-dict shapes defined in the HTTP contract consumed by the dashboard UI.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any


def _num(v: Any) -> Any:
    """Decimal/number -> float; pass through None."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def _enum(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Enum):
        return v.value
    return v


def _dt(v: Any) -> Any:
    if v is None:
        return None
    return v.isoformat()


def jsonable(v: Any) -> Any:
    """Recursively coerce Decimal/datetime/enum inside dicts & lists for JSON.

    Used for service helpers (ledger_summary, strategy_performance) that return
    plain dicts containing Decimal values.
    """
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, dict):
        return {k: jsonable(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def position_to_dict(position) -> dict:
    strategy_slug = None
    # Only read the relationship if it was loaded to avoid a lazy DB hit.
    strat = getattr(position, "strategy", None)
    if strat is not None:
        strategy_slug = strat.slug
    return {
        "id": position.id,
        "symbol": position.symbol,
        "side": _enum(position.side),
        "status": _enum(position.status),
        "qty": _num(position.qty),
        "avg_entry_px": _num(position.avg_entry_px),
        "realized_pnl": _num(position.realized_pnl),
        "strategy_id": position.strategy_id,
        "strategy_slug": strategy_slug,
        "opened_at": _dt(position.opened_at),
        "closed_at": _dt(position.closed_at),
        "notes": position.notes,
    }


def event_to_dict(event) -> dict:
    return {
        "id": event.id,
        "position_id": event.position_id,
        "strategy_id": event.strategy_id,
        "action": _enum(event.action),
        "symbol": event.symbol,
        "qty": _num(event.qty),
        "price": _num(event.price),
        "fees": _num(event.fees),
        "realized_pnl": _num(event.realized_pnl),
        "batch_id": event.batch_id,
        "ts": _dt(event.ts),
        "notes": event.notes,
    }


def backtest_to_dict(backtest) -> dict:
    strategy_slug = None
    strat = getattr(backtest, "strategy", None)
    if strat is not None:
        strategy_slug = strat.slug
    return {
        "id": backtest.id,
        "strategy_id": backtest.strategy_id,
        "strategy_slug": strategy_slug,
        "symbol": backtest.symbol,
        "interval": backtest.interval,
        "start_date": _dt(backtest.start_date),
        "end_date": _dt(backtest.end_date),
        "n_trades": backtest.n_trades,
        "win_rate": _num(backtest.win_rate),
        "avg_r": _num(backtest.avg_r),
        "sharpe": _num(backtest.sharpe),
        "max_dd": _num(backtest.max_dd),
        "cagr": _num(backtest.cagr),
        "total_r": _num(backtest.total_r),
        "placebo_pass": backtest.placebo_pass,
        "created_at": _dt(backtest.created_at),
        "notes": backtest.notes,
    }


def strategy_to_dict(strategy) -> dict:
    backtests = getattr(strategy, "backtests", None)
    n_backtests = len(backtests) if backtests is not None else 0
    return {
        "id": strategy.id,
        "slug": strategy.slug,
        "name": strategy.name,
        "status": _enum(strategy.status),
        "description": strategy.description,
        "params": strategy.params,
        "n_backtests": n_backtests,
    }
