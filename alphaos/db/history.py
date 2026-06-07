"""Reconstruct a daily NAV-index series from the transaction ledger + history.

The persisted nav_snapshots ledger only grows from the day the snapshot job first
runs, so a fresh deployment has no past to chart. But the past is fully derivable:
for each trading day we know the holdings (aggregate the transactions dated on/before
that day), the close price that day (MinIO daily bars), the cash position (the same
reconciliation used live), and any contributions (cash-flow ledger). Chaining those
through the shared per-period math yields the historical NAV index / drawdown curve.

Holdings whose symbol is not in MinIO (e.g. EUR UCITS ETFs) are valued at cost on
every day, so they contribute no spurious daily P&L. FX uses the current cached rate
(a good approximation over short windows; refine with a historical FX series later).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import pricing
from .allocation import list_holdings
from .cash_flows import net_flow_between
from .config import get_config
from .fx import fx_to_sek
from .models import DeleverStatus, NavSnapshot
from .nav import _compute_period_metrics
from .reconcile import account_cash
from .transactions import aggregate, list_transactions

_ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_date(value: dt.date | str) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _symbol_map(holdings, txns) -> dict[str, str]:
    """ISIN -> ticker symbol, from holdings first then transaction rows."""
    by_isin: dict[str, str] = {}
    for h in holdings:
        if h.isin and h.symbol:
            by_isin[h.isin.upper()] = h.symbol.upper()
    for t in txns:
        key = (t.isin or "").upper()
        if key and key not in by_isin and t.symbol:
            by_isin[key] = t.symbol.upper()
    return by_isin


def reconstruct_series(
    session: Session,
    *,
    start: dt.date | str | None = None,
    end: dt.date | str | None = None,
    closes: dict[dt.date, dict[str, Decimal]] | None = None,
) -> list[dict[str, Any]]:
    """Reconstruct the daily NAV series over [start, end] (defaults: first
    transaction .. today). One row per trading day with a close in range, shaped
    like serialize.nav_snapshot_to_dict (id/notes are None).

    `closes` may be injected ({date: {ticker: close}}) to avoid MinIO in tests.
    Returns [] when there is no price history (so callers fall back to the
    persisted ledger rather than charting a fabricated flat line).
    """
    cfg = get_config(session)
    txns = list_transactions(session)
    if not txns:
        return []

    start = _as_date(start) if start else min(t.date for t in txns)
    end = _as_date(end) if end else dt.date.today()
    if end < start:
        return []

    holdings = list_holdings(session)
    symbol_by_isin = _symbol_map(holdings, txns)
    symbols = sorted(set(symbol_by_isin.values()))

    if closes is None:
        # Buffer the window back a few days so the first point has a prior close.
        closes = pricing.closes_in_range(symbols, start - dt.timedelta(days=10), end)
    if not closes:
        return []

    # Per-symbol ascending (date, close) for forward-filled lookups.
    series_by_symbol: dict[str, list[tuple[dt.date, Decimal]]] = {}
    for d in sorted(closes):
        for sym, px in closes[d].items():
            series_by_symbol.setdefault(sym, []).append((d, _dec(px)))

    def price_on(sym: str | None, day: dt.date) -> Decimal | None:
        ser = series_by_symbol.get(sym or "")
        if not ser:
            return None
        chosen: Decimal | None = None
        for d, px in ser:
            if d <= day:
                chosen = px
            else:
                break
        return chosen

    point_dates = sorted({d for d in closes if start <= d <= end} | {start, end})

    rows: list[dict[str, Any]] = []
    prev: NavSnapshot | None = None
    prev_date = dt.date.min
    for day in point_dates:
        positions = aggregate(t for t in txns if t.date <= day)
        securities = _ZERO
        for isin, p in positions.items():
            fx = fx_to_sek(cfg, p["currency"])
            px = price_on(symbol_by_isin.get(isin), day)
            if px is None:
                px = _dec(p["avg_price"])  # no close -> value at cost (no fake P&L)
            securities += _dec(p["qty"]) * px * fx

        cash = account_cash(session, through=day)
        loan = max(_ZERO, -cash)
        cash_asset = max(_ZERO, cash)
        gross = securities + cash_asset
        equity = gross - loan
        contribution = net_flow_between(session, prev_date, day)
        prev_date = day
        if equity <= _ZERO:
            continue  # before any net long exposure exists

        m = _compute_period_metrics(
            cfg, gross=gross, loan=loan, equity=equity, contribution=contribution, prev=prev
        )
        rows.append({
            "id": None,
            "as_of": day.isoformat(),
            "gross_asset_value": float(gross),
            "loan_balance": float(loan),
            "net_contribution": float(contribution),
            "equity": float(equity),
            "twr_period": float(m["twr"]) if m["twr"] is not None else None,
            "nav_index": float(m["nav_index"]),
            "peak_nav_index": float(m["peak_nav_index"]),
            "drawdown": float(m["drawdown"]),
            "effective_leverage": float(m["effective_leverage"]) if m["effective_leverage"] is not None else None,
            "belaningsgrad": float(m["belaningsgrad"]) if m["belaningsgrad"] is not None else None,
            "delever_status": m["delever_status"].value,
            "notes": None,
        })
        # Transient (not added to the session) prev for the next period's math.
        prev = NavSnapshot(
            equity=equity, nav_index=m["nav_index"], peak_nav_index=m["peak_nav_index"]
        )
    return rows


def backfill_snapshots(
    session: Session,
    *,
    start: dt.date | str | None = None,
    end: dt.date | str | None = None,
) -> int:
    """Persist the reconstructed series as nav_snapshots, replacing any existing
    rows in the covered date range. Returns the number of snapshots written."""
    rows = reconstruct_series(session, start=start, end=end)
    if not rows:
        return 0
    dmin = dt.date.fromisoformat(rows[0]["as_of"])
    dmax = dt.date.fromisoformat(rows[-1]["as_of"])
    session.execute(
        delete(NavSnapshot).where(NavSnapshot.as_of >= dmin, NavSnapshot.as_of <= dmax)
    )
    session.flush()
    for r in rows:
        session.add(NavSnapshot(
            as_of=dt.date.fromisoformat(r["as_of"]),
            gross_asset_value=_dec(r["gross_asset_value"]),
            loan_balance=_dec(r["loan_balance"]),
            net_contribution=_dec(r["net_contribution"]),
            equity=_dec(r["equity"]),
            twr_period=_dec(r["twr_period"]) if r["twr_period"] is not None else None,
            nav_index=_dec(r["nav_index"]),
            peak_nav_index=_dec(r["peak_nav_index"]),
            drawdown=_dec(r["drawdown"]),
            effective_leverage=_dec(r["effective_leverage"]) if r["effective_leverage"] is not None else None,
            belaningsgrad=_dec(r["belaningsgrad"]) if r["belaningsgrad"] is not None else None,
            delever_status=DeleverStatus(r["delever_status"]),
            notes="reconstructed",
        ))
    session.flush()
    return len(rows)
