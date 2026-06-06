"""NAV-index ledger services — the heart of the V2-FRONTIER risk model.

The NAV index is a time-weighted return (TWR) series computed on *equity*
(gross assets - loan) and stripped of contribution flows, so it reflects strategy
performance, not deposits. Drawdown is measured off the NAV index, and the binding
de-lever rule (repay half at -35%, all at -45%) is evaluated against it.

Period return (simple Dietz, single net flow per period):
    r = (equity_end - equity_begin - net_contribution) / equity_begin
    nav_index = prev_nav_index * (1 + r)
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .allocation import total_gross_value
from .config import get_config, target_leverage
from .models import DeleverStatus, NavSnapshot

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_date(value: dt.date | str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def latest_snapshot(session: Session, before: dt.date | None = None) -> NavSnapshot | None:
    stmt = select(NavSnapshot).order_by(NavSnapshot.as_of.desc())
    if before is not None:
        stmt = stmt.where(NavSnapshot.as_of < before)
    return session.scalars(stmt).first()


def list_snapshots(session: Session, limit: int | None = None) -> list[NavSnapshot]:
    stmt = select(NavSnapshot).order_by(NavSnapshot.as_of)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def _delever_status(cfg, drawdown: Decimal) -> DeleverStatus:
    dd = _dec(drawdown)
    if dd <= _dec(cfg.delever_full_dd):
        return DeleverStatus.full
    if dd <= _dec(cfg.delever_half_dd):
        return DeleverStatus.half
    return DeleverStatus.normal


def add_snapshot(
    session: Session,
    *,
    as_of: dt.date | str,
    gross_asset_value: Any = None,
    loan_balance: Any = 0,
    net_contribution: Any = 0,
    notes: str | None = None,
) -> NavSnapshot:
    """Record a NAV-ledger observation and derive TWR / NAV-index / drawdown /
    leverage / belaningsgrad / de-lever status from it.

    If gross_asset_value is omitted it is summed from current holdings.
    """
    when = _as_date(as_of)
    gross = total_gross_value(session) if gross_asset_value is None else _dec(gross_asset_value)
    loan = _dec(loan_balance)
    contrib = _dec(net_contribution)
    equity = gross - loan
    if equity <= _ZERO:
        raise ValueError("equity (gross - loan) must be > 0")

    cfg = get_config(session)
    prev = latest_snapshot(session, before=when)

    if prev is not None and _dec(prev.equity) > _ZERO:
        twr: Decimal | None = (equity - _dec(prev.equity) - contrib) / _dec(prev.equity)
        nav_index = _dec(prev.nav_index) * (_ONE + twr)
        peak = max(_dec(prev.peak_nav_index), nav_index)
    else:
        twr = None          # first observation: baseline the index at 1.0
        nav_index = _ONE
        peak = _ONE

    drawdown = (nav_index / peak - _ONE) if peak > _ZERO else _ZERO
    eff_lev = (gross / equity) if equity > _ZERO else None
    belan = (loan / gross) if gross > _ZERO else _ZERO

    snap = NavSnapshot(
        as_of=when,
        gross_asset_value=gross,
        loan_balance=loan,
        net_contribution=contrib,
        equity=equity,
        twr_period=twr,
        nav_index=nav_index,
        peak_nav_index=peak,
        drawdown=drawdown,
        effective_leverage=eff_lev,
        belaningsgrad=belan,
        delever_status=_delever_status(cfg, drawdown),
        notes=notes,
    )
    session.add(snap)
    session.flush()
    return snap


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


def current_risk(session: Session) -> dict[str, Any]:
    """Assemble the live risk picture for the dashboard: NAV/drawdown, de-lever
    distances, leverage vs glide target, belaningsgrad headroom, planning case."""
    cfg = get_config(session)
    snap = latest_snapshot(session)

    thresholds = {
        "delever_half_dd": _f(cfg.delever_half_dd),
        "delever_full_dd": _f(cfg.delever_full_dd),
        "forced_sale_dd": _f(cfg.forced_sale_dd),
        "belaningsgrad_cliff": _f(cfg.belaningsgrad_cliff),
        "reentry_recovery": _f(cfg.reentry_recovery),
    }
    base = {
        "as_of": None,
        "nav_index": None,
        "drawdown": None,
        "twr_period": None,
        "gross_asset_value": None,
        "loan_balance": None,
        "equity": None,
        "effective_leverage": None,
        "belaningsgrad": None,
        "delever_status": "normal",
        "thresholds": thresholds,
        "external_reserve": _f(cfg.external_reserve),
        "planning_cagr": [_f(cfg.planning_cagr_low), _f(cfg.planning_cagr_high)],
        "account_label": cfg.account_label,
        "base_currency": cfg.base_currency,
        "action": "no NAV snapshots yet",
    }
    if snap is None:
        base["target_leverage"] = _f(cfg.leverage_target)
        return base

    dd = _dec(snap.drawdown)
    belan = _dec(snap.belaningsgrad or 0)
    equity = _dec(snap.equity)
    status = snap.delever_status.value

    action = {
        "normal": "none — within tolerance",
        "half": "repay HALF the loan within 3 trading days (-35% NAV-index DD breached)",
        "full": "repay the ENTIRE loan within 3 trading days (-45% NAV-index DD breached)",
        "reentry": "re-entering: re-lever in two halves across two monthly rebalances",
    }.get(status, "none")

    base.update({
        "as_of": snap.as_of.isoformat(),
        "nav_index": _f(snap.nav_index),
        "drawdown": _f(snap.drawdown),
        "twr_period": _f(snap.twr_period),
        "gross_asset_value": _f(snap.gross_asset_value),
        "loan_balance": _f(snap.loan_balance),
        "equity": _f(snap.equity),
        "effective_leverage": _f(snap.effective_leverage),
        "belaningsgrad": _f(snap.belaningsgrad),
        "delever_status": status,
        # Headroom = how much more adverse before the trigger (positive = room left).
        "headroom_to_half": _f(dd - _dec(cfg.delever_half_dd)),
        "headroom_to_full": _f(dd - _dec(cfg.delever_full_dd)),
        "headroom_to_forced_sale": _f(dd - _dec(cfg.forced_sale_dd)),
        "belaningsgrad_headroom": _f(_dec(cfg.belaningsgrad_cliff) - belan),
        "target_leverage": _f(target_leverage(cfg, equity)),
        "action": action,
    })
    return base
