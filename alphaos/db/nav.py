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

from .allocation import portfolio_pnl, total_gross_value
from .cash_flows import net_flow_between
from .config import get_config, target_leverage
from .models import DeleverStatus, NavSnapshot
from .reconcile import account_position

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


def _compute_period_metrics(
    cfg,
    *,
    gross: Decimal,
    loan: Decimal,
    equity: Decimal,
    contribution: Decimal,
    prev: NavSnapshot | None,
) -> dict[str, Any]:
    """Pure per-period NAV math (no DB / no side effects). Shared by add_snapshot,
    upsert_snapshot, and the live current_risk path.

    Period return is simple Dietz with a single net flow per period; the index is
    baselined at 1.0 on the first observation (no prior equity to compare against).
    """
    if prev is not None and _dec(prev.equity) > _ZERO:
        twr: Decimal | None = (equity - _dec(prev.equity) - contribution) / _dec(prev.equity)
        nav_index = _dec(prev.nav_index) * (_ONE + twr)
        peak = max(_dec(prev.peak_nav_index), nav_index)
    else:
        twr = None          # first observation: baseline the index at 1.0
        nav_index = _ONE
        peak = _ONE

    drawdown = (nav_index / peak - _ONE) if peak > _ZERO else _ZERO
    eff_lev = (gross / equity) if equity > _ZERO else None
    belan = (loan / gross) if gross > _ZERO else _ZERO

    return {
        "twr": twr,
        "nav_index": nav_index,
        "peak_nav_index": peak,
        "drawdown": drawdown,
        "effective_leverage": eff_lev,
        "belaningsgrad": belan,
        "delever_status": _delever_status(cfg, drawdown),
    }


def _derive_gross_loan(
    session: Session, as_of: dt.date, gross_override: Any, loan_override: Any
) -> tuple[Decimal, Decimal]:
    """Derive (gross, loan) as-of `as_of` from the ledgers; explicit args override.

    gross = securities @ latest close (total_gross_value) + idle cash (cash_asset);
    loan  = account_position(through=as_of).loan (overdraw against the account).
    """
    pos = account_position(session, through=as_of)
    gross = (
        total_gross_value(session) + pos["cash_asset"]
        if gross_override is None
        else _dec(gross_override)
    )
    loan = pos["loan"] if loan_override is None else _dec(loan_override)
    return gross, loan


def add_snapshot(
    session: Session,
    *,
    as_of: dt.date | str,
    gross_asset_value: Any = None,
    loan_balance: Any = None,
    net_contribution: Any = None,
    notes: str | None = None,
) -> NavSnapshot:
    """Record a NAV-ledger observation and derive TWR / NAV-index / drawdown /
    leverage / belaningsgrad / de-lever status from it.

    All three measurements default to None and are DERIVED from the ledgers:
        gross = total_gross_value(session) [securities @ latest close] + idle cash
                (account_position.cash_asset).
        loan  = account_position(session, through=as_of).loan (overdraw).
        net_contribution = cash-flow net over this snapshot's period
                (prev.as_of EXCLUSIVE .. when INCLUSIVE; dt.date.min lower bound for
                the first snapshot captures all flows since inception).
    Any explicitly-passed value overrides its derivation.
    """
    when = _as_date(as_of)
    cfg = get_config(session)
    prev = latest_snapshot(session, before=when)

    gross, loan = _derive_gross_loan(session, when, gross_asset_value, loan_balance)
    if net_contribution is None:
        contrib = net_flow_between(session, prev.as_of if prev is not None else dt.date.min, when)
    else:
        contrib = _dec(net_contribution)

    equity = gross - loan
    if equity <= _ZERO:
        raise ValueError("equity (gross - loan) must be > 0")

    m = _compute_period_metrics(
        cfg, gross=gross, loan=loan, equity=equity, contribution=contrib, prev=prev
    )

    snap = NavSnapshot(
        as_of=when,
        gross_asset_value=gross,
        cost_basis=portfolio_pnl(session)["cost_basis"],
        loan_balance=loan,
        net_contribution=contrib,
        equity=equity,
        twr_period=m["twr"],
        nav_index=m["nav_index"],
        peak_nav_index=m["peak_nav_index"],
        drawdown=m["drawdown"],
        effective_leverage=m["effective_leverage"],
        belaningsgrad=m["belaningsgrad"],
        delever_status=m["delever_status"],
        notes=notes,
    )
    session.add(snap)
    session.flush()
    return snap


def upsert_snapshot(
    session: Session,
    *,
    as_of: dt.date | str | None = None,
    gross_asset_value: Any = None,
    loan_balance: Any = None,
    net_contribution: Any = None,
    notes: str | None = None,
) -> NavSnapshot:
    """Like add_snapshot but REPLACES any existing snapshot for `as_of` (idempotent).

    The daily snapshot job uses this so re-runs for the same trading day are safe.
    Defaults `as_of` to today.
    """
    when = _as_date(as_of) if as_of is not None else dt.date.today()
    existing = session.scalar(select(NavSnapshot).where(NavSnapshot.as_of == when))
    if existing is not None:
        session.delete(existing)
        session.flush()  # clear the unique as_of before re-insert
    return add_snapshot(
        session,
        as_of=when,
        gross_asset_value=gross_asset_value,
        loan_balance=loan_balance,
        net_contribution=net_contribution,
        notes=notes,
    )


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


def _nav_index_reliability(
    session: Session, pnl: dict[str, Any], persisted_count: int, today: dt.date
) -> tuple[bool, str | None]:
    """The time-weighted NAV index is only trustworthy when contributions are
    fully recorded and there is real forward history. Flag (and explain) when not,
    so the dashboard shows '—' instead of a misleading number."""
    reasons: list[str] = []
    if persisted_count < 2:
        reasons.append("history reconstructed from a short price window")
    recorded = net_flow_between(session, dt.date.min, today)  # total deposits/withdrawals
    cost = pnl["cost_basis"]
    if cost > _ZERO and abs(recorded) < cost * Decimal("0.5"):
        reasons.append("deposit history looks incomplete")
    return (not reasons), ("; ".join(reasons) or None)


def compute_cagr_since_inception(
    session: Session, nav_index_live: Any, today: dt.date, *, reliable: bool
) -> tuple[float | None, str | None]:
    """Annualized TWR since the first snapshot.

    The NAV index is baselined at 1.0 on the first snapshot, so the live index IS
    the cumulative growth factor since inception. Returns (cagr, inception_iso).
    cagr is None (but inception is still returned) when the index isn't trustworthy
    or there isn't enough elapsed history (< ~30 days) to annualize meaningfully.
    """
    snaps = list_snapshots(session)
    if not snaps:
        return None, None
    inception = snaps[0].as_of
    iso = inception.isoformat()
    days = (today - inception).days
    if not reliable or days < 30 or nav_index_live is None:
        return None, iso
    nav = float(_dec(nav_index_live))
    if nav <= 0:
        return None, iso
    years = days / 365.25
    return nav ** (1.0 / years) - 1.0, iso


def current_risk(session: Session) -> dict[str, Any]:
    """Assemble the live risk picture for the dashboard: NAV/drawdown, de-lever
    distances, leverage vs glide target, belaningsgrad headroom, CAGR since inception.

    The current reading is computed LIVE off the latest closes — gross/loan/equity
    are derived as-of today (as in add_snapshot) and the per-period math runs against
    the latest PERSISTED snapshot strictly before today, WITHOUT persisting. So the
    tile reflects today's prices between cron runs; the persisted daily series (from
    the job) supplies the historical peak/drawdown via /api/nav.
    """
    cfg = get_config(session)

    thresholds = {
        "delever_half_dd": _f(cfg.delever_half_dd),
        "delever_full_dd": _f(cfg.delever_full_dd),
        "forced_sale_dd": _f(cfg.forced_sale_dd),
        "belaningsgrad_cliff": _f(cfg.belaningsgrad_cliff),
        "reentry_recovery": _f(cfg.reentry_recovery),
        "delever_floor_leverage": _f(cfg.delever_floor_leverage),
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
        "cagr_since_inception": None,
        "inception_date": None,
        "account_label": cfg.account_label,
        "base_currency": cfg.base_currency,
        "action": "no NAV snapshots yet",
        "pnl": None,
        "nav_index_reliable": False,
        "nav_index_note": None,
    }

    # Money-terms P&L (works with no snapshots) + whether the time-weighted index
    # can be trusted; both are returned even in the empty-state below.
    today = dt.date.today()
    pnl = portfolio_pnl(session, sleeve_only=True)
    nav_reliable, nav_note = _nav_index_reliability(
        session, pnl, len(list_snapshots(session)), today
    )
    base["pnl"] = {
        "market_value": _f(pnl["market_value"]),
        "cost_basis": _f(pnl["cost_basis"]),
        "unrealized_pnl": _f(pnl["unrealized_pnl"]),
        "return_pct": _f(pnl["return_pct"]),
        "priced": pnl["priced"],
        "at_cost": pnl["at_cost"],
    }
    base["nav_index_reliable"] = nav_reliable
    base["nav_index_note"] = nav_note

    # Live reading: derive gross/loan/equity as-of today and run the shared period
    # math against the latest snapshot strictly before today (no persistence).
    gross, loan = _derive_gross_loan(session, today, None, None)
    prev = latest_snapshot(session, before=today)
    equity = gross - loan
    if (gross <= _ZERO and prev is None) or equity <= _ZERO:
        base["target_leverage"] = _f(cfg.leverage_target)
        return base

    m = _compute_period_metrics(
        cfg, gross=gross, loan=loan, equity=equity, contribution=_ZERO, prev=prev
    )
    dd = m["drawdown"]
    belan = m["belaningsgrad"]
    status = m["delever_status"].value

    action = {
        "normal": "none — within tolerance",
        "half": "repay HALF the loan within 3 trading days (-35% NAV-index DD breached)",
        "full": "repay the ENTIRE loan within 3 trading days (-45% NAV-index DD breached)",
        "reentry": "re-entering: re-lever in two halves across two monthly rebalances",
    }.get(status, "none")

    base.update({
        "as_of": today.isoformat(),
        "nav_index": _f(m["nav_index"]),
        "drawdown": _f(dd),
        "twr_period": _f(m["twr"]),
        "gross_asset_value": _f(gross),
        "loan_balance": _f(loan),
        "equity": _f(equity),
        "effective_leverage": _f(m["effective_leverage"]),
        "belaningsgrad": _f(belan),
        "delever_status": status,
        # Headroom = how much more adverse before the trigger (positive = room left).
        "headroom_to_half": _f(dd - _dec(cfg.delever_half_dd)),
        "headroom_to_full": _f(dd - _dec(cfg.delever_full_dd)),
        "headroom_to_forced_sale": _f(dd - _dec(cfg.forced_sale_dd)),
        "belaningsgrad_headroom": _f(_dec(cfg.belaningsgrad_cliff) - belan),
        "target_leverage": _f(target_leverage(cfg, equity)),
        "action": action,
    })

    # Annualized return since inception (TWR), from the NAV index baselined at 1.0
    # on the first snapshot. Only shown when the index is trustworthy and there is
    # enough elapsed history; otherwise None ("—" on the dashboard).
    cagr, inception = compute_cagr_since_inception(
        session, m["nav_index"], today, reliable=nav_reliable
    )
    base["cagr_since_inception"] = cagr
    base["inception_date"] = inception
    return base
