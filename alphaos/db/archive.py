"""Strategy + backtest archive — services to persist and query the backlog.

`strategies` is the catalog of approaches tried; `backtests` stores each run's
performance so you can compare how a strategy did across time / parameters.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Backtest, Strategy, StrategyStatus


def _as_status(status: StrategyStatus | str | None) -> StrategyStatus | None:
    if status is None:
        return None
    return status if isinstance(status, StrategyStatus) else StrategyStatus(str(status))


def get_strategy(session: Session, slug: str) -> Strategy | None:
    return session.scalars(select(Strategy).where(Strategy.slug == slug)).first()


def upsert_strategy(
    session: Session,
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
    status: StrategyStatus | str | None = None,
    params: dict | None = None,
) -> Strategy:
    """Create the strategy if missing, else update the provided fields."""
    strat = get_strategy(session, slug)
    if strat is None:
        strat = Strategy(slug=slug, name=name or slug)
        session.add(strat)
    if name is not None:
        strat.name = name
    if description is not None:
        strat.description = description
    st = _as_status(status)
    if st is not None:
        strat.status = st
    if params is not None:
        strat.params = params
    session.flush()
    return strat


def seed_strategies_from_setups(session: Session) -> int:
    """Seed/refresh the strategies catalog from alphaos.setups.SETUPS.

    Idempotent: existing strategies (matched by slug) are updated, not duplicated.
    Returns the number of strategies seeded.
    """
    from ..setups import SETUPS

    n = 0
    for slug, spec in SETUPS.items():
        upsert_strategy(
            session,
            slug,
            name=getattr(spec, "name", slug),
            description=getattr(spec, "description", None),
            status=StrategyStatus.active,
        )
        n += 1
    return n


def save_backtest(
    session: Session,
    *,
    strategy_slug: str,
    symbol: str,
    interval: str,
    n_trades: int = 0,
    win_rate: float | None = None,
    avg_r: float | None = None,
    sharpe: float | None = None,
    max_dd: float | None = None,
    cagr: float | None = None,
    total_r: float | None = None,
    placebo_pass: bool | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    params: dict | None = None,
    equity_curve: dict | None = None,
    notes: str | None = None,
) -> Backtest:
    """Persist one backtest run, creating the strategy by slug if needed."""
    strat = get_strategy(session, strategy_slug)
    if strat is None:
        strat = upsert_strategy(session, strategy_slug, status=StrategyStatus.experimental)
    bt = Backtest(
        strategy_id=strat.id,
        symbol=symbol.strip().upper(),
        interval=interval,
        start_date=start_date,
        end_date=end_date,
        params=params,
        n_trades=n_trades,
        win_rate=win_rate,
        avg_r=avg_r,
        sharpe=sharpe,
        max_dd=max_dd,
        cagr=cagr,
        total_r=total_r,
        placebo_pass=placebo_pass,
        equity_curve=equity_curve,
        notes=notes,
    )
    session.add(bt)
    session.flush()
    return bt


def list_strategies(session: Session) -> list[Strategy]:
    return list(session.scalars(select(Strategy).order_by(Strategy.name)))


def list_backtests(
    session: Session, strategy_id: int | None = None, limit: int = 100
) -> list[Backtest]:
    stmt = select(Backtest).order_by(Backtest.created_at.desc()).limit(limit)
    if strategy_id is not None:
        stmt = stmt.where(Backtest.strategy_id == strategy_id)
    return list(session.scalars(stmt))


def strategy_performance(session: Session) -> list[dict[str, Any]]:
    """Per-strategy summary: run count + the most recent run's headline metrics."""
    out: list[dict[str, Any]] = []
    for strat in list_strategies(session):
        runs = list_backtests(session, strategy_id=strat.id, limit=1)
        latest = runs[0] if runs else None
        n_backtests = session.scalar(
            select(func.count(Backtest.id)).where(Backtest.strategy_id == strat.id)
        )
        out.append(
            {
                "id": strat.id,
                "slug": strat.slug,
                "name": strat.name,
                "status": strat.status.value,
                "n_backtests": int(n_backtests or 0),
                "latest": _backtest_metrics(latest) if latest else None,
            }
        )
    return out


def _backtest_metrics(bt: Backtest) -> dict[str, Any]:
    def f(v: Any) -> float | None:
        return float(v) if v is not None else None

    return {
        "id": bt.id,
        "symbol": bt.symbol,
        "interval": bt.interval,
        "n_trades": bt.n_trades,
        "win_rate": f(bt.win_rate),
        "avg_r": f(bt.avg_r),
        "sharpe": f(bt.sharpe),
        "max_dd": f(bt.max_dd),
        "cagr": f(bt.cagr),
        "total_r": f(bt.total_r),
        "placebo_pass": bt.placebo_pass,
        "created_at": bt.created_at.isoformat() if bt.created_at else None,
    }
