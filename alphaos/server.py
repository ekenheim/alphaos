"""FastAPI backend serving the AlphaOS dashboard.

Endpoints:
  GET /api/health          health probe (lightweight; used by k8s probes)
  GET /api/status          data-source + DB diagnostics (live checks)
  GET /api/portfolio       META-EA strategy summary (KPIs + equity + monthly heatmap)
  GET /api/propfirm        prop-firm portfolio summary (10 sim accounts)
  GET /api/strategies      per-instrument strategy comparison
  GET /api/ledger/*        live positions + trade-event ledger
  GET /api/archive/*       strategy + backtest archive
  GET /                    serves the static dashboard

Run:  python -m alphaos.cli serve   (or:  uvicorn alphaos.server:app --port 8503)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .portfolio import (
    PROP_FIRMS, STRATEGY_SYMBOLS, portfolio_summary_json,
    prop_portfolio_summary_json, run_portfolio, simulate_prop_portfolio,
)
from .db import session_scope, have_database
from .db import ledger as dbledger
from .db import archive as dbarchive
from .db.serialize import (
    position_to_dict, event_to_dict, backtest_to_dict, strategy_to_dict,
    jsonable,
)

_DB_UNCONFIGURED = JSONResponse(status_code=503, content={"error": "database not configured"})

try:
    from . import __version__ as APP_VERSION
except Exception:  # pragma: no cover
    APP_VERSION = "unknown"

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="AlphaOS — META-EA prop dashboard")

app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


# --- Cached compute (recompute on a slow cadence so the UI is snappy) ---

class _Cache:
    def __init__(self, ttl_seconds: float = 300):
        self.ttl = ttl_seconds
        self._lock = Lock()
        self._runs = None
        self._accounts = None
        self._timestamp = 0.0

    def get(self):
        with self._lock:
            if (time.time() - self._timestamp) > self.ttl or self._runs is None:
                runs = run_portfolio()
                accounts = simulate_prop_portfolio(runs)
                self._runs = runs
                self._accounts = accounts
                self._timestamp = time.time()
            return self._runs, self._accounts


_cache = _Cache()


# --- Routes ---

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/portfolio")
def portfolio() -> JSONResponse:
    runs, _ = _cache.get()
    return JSONResponse(portfolio_summary_json(runs))


@app.get("/api/propfirm")
def propfirm() -> JSONResponse:
    _, accounts = _cache.get()
    return JSONResponse(prop_portfolio_summary_json(accounts))


@app.get("/api/status")
def status() -> JSONResponse:
    """Diagnostics: which market-data source is active + DB connectivity.

    Heavier than /api/health (does live reachability checks) — NOT used by the
    k8s liveness/readiness probes, which hit /api/health.
    """
    from . import minio as zmin
    from .db import engine as dbengine

    minio_enabled = os.getenv("ALPHAOS_USE_MINIO") == "1"
    minio_creds = zmin.have_credentials()
    data_source = {
        "active": "minio" if (minio_enabled and minio_creds) else "yfinance",
        "minio_enabled": minio_enabled,
        "minio_credentials": minio_creds,
        "minio_endpoint": zmin.endpoint(),
        "minio_bucket": zmin.bucket(),
        "minio_reachable": zmin.check_reachable() if minio_enabled else None,
    }
    return JSONResponse({
        "ok": True,
        "version": APP_VERSION,
        "data_source": data_source,
        "database": dbengine.db_status(),
    })


# --- Ledger (live positions + trade-event ledger) ---

@app.get("/api/ledger/positions")
def ledger_positions() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        positions = dbledger.open_positions(session)
        summary = dbledger.ledger_summary(session)
        return JSONResponse({
            "positions": [position_to_dict(p) for p in positions],
            "summary": jsonable(summary),
        })


@app.get("/api/ledger/positions/{position_id}")
def ledger_position_detail(position_id: int) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        position = dbledger.position_detail(session, position_id)
        if position is None:
            return JSONResponse(status_code=404, content={"error": "position not found"})
        return JSONResponse({
            "position": position_to_dict(position),
            "events": [event_to_dict(e) for e in position.events],
        })


@app.post("/api/ledger/execute")
async def ledger_execute(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            event = dbledger.record_execution(
                session,
                symbol=body["symbol"],
                action=body["action"],
                qty=body["qty"],
                price=body["price"],
                side=body.get("side", "long"),
                strategy_id=body.get("strategy_id"),
                fees=body.get("fees", 0),
                notes=body.get("notes"),
                position_id=body.get("position_id"),
            )
            position = event.position
            payload = {
                "event": event_to_dict(event),
                "position": position_to_dict(position) if position is not None else None,
            }
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/ledger/rebalance")
async def ledger_rebalance(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            events = dbledger.rebalance(
                session,
                body.get("legs", []),
                note=body.get("note"),
            )
            batch_id = events[0].batch_id if events else None
            # Affected positions, de-duplicated by id, in stable order.
            seen: dict = {}
            for ev in events:
                pos = ev.position
                if pos is not None and pos.id not in seen:
                    seen[pos.id] = pos
            payload = {
                "batch_id": batch_id,
                "events": [event_to_dict(e) for e in events],
                "positions": [position_to_dict(p) for p in seen.values()],
            }
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# --- Archive (strategies + backtest results) ---

@app.get("/api/archive/strategies")
def archive_strategies() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        strategies = dbarchive.list_strategies(session)
        return JSONResponse({
            "strategies": [strategy_to_dict(s) for s in strategies],
        })


@app.post("/api/archive/strategies")
async def archive_upsert_strategy(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            strategy = dbarchive.upsert_strategy(
                session,
                body["slug"],
                name=body.get("name"),
                description=body.get("description"),
                status=body.get("status"),
                params=body.get("params"),
            )
            payload = {"strategy": strategy_to_dict(strategy)}
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/archive/performance")
def archive_performance() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        performance = dbarchive.strategy_performance(session)
        return JSONResponse({"performance": jsonable(performance)})


@app.get("/api/archive/backtests")
def archive_backtests(strategy_id: int | None = None) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        backtests = dbarchive.list_backtests(session, strategy_id=strategy_id)
        return JSONResponse({
            "backtests": [backtest_to_dict(b) for b in backtests],
        })


@app.get("/api/strategies")
def strategies() -> JSONResponse:
    runs, _ = _cache.get()
    out = []
    for r in runs:
        res = r.result
        out.append(dict(
            symbol=r.symbol,
            setup=r.setup,
            trades=res.n_trades,
            win_rate=res.win_rate,
            avg_r=res.avg_r,
            sharpe=res.sharpe,
            max_dd=res.max_dd,
            cagr=res.cagr,
        ))
    return JSONResponse({"strategies": out, "symbols": STRATEGY_SYMBOLS, "firms": PROP_FIRMS})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/{page}.html")
def page(page: str) -> FileResponse:
    path = WEB_DIR / f"{page}.html"
    if not path.exists():
        return FileResponse(WEB_DIR / "index.html")
    return FileResponse(path)
