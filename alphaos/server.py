"""FastAPI backend serving the AlphaOS V2-FRONTIER dashboard.

Endpoints:
  GET  /api/health         health probe (lightweight; used by k8s probes)
  GET  /api/status         version + DB diagnostics
  GET  /api/config         portfolio config (singleton)
  POST /api/config         update editable config fields
  GET  /api/sleeves        list sleeves
  POST /api/sleeves        upsert a sleeve
  GET  /api/holdings       list holdings
  POST /api/holdings       upsert a holding
  DELETE /api/holdings/{id} delete a holding
  GET  /api/allocation     allocation breakdown (JSON-native)
  GET  /api/nav            NAV snapshots + current risk
  POST /api/nav            add a NAV snapshot
  POST /api/nav/snapshot-now  upsert a derived snapshot for today (idempotent)
  POST /api/nav/backfill   reconstruct + persist the daily NAV series from history
  GET  /api/nav/journal    daily money journal (value/cost/day-P&L/events)
  GET  /api/nav/holdings-on?date=  holdings held on a date, with valuation
  GET  /api/cashflows      list cash flows (deposits/withdrawals)
  POST /api/cashflows      add a cash flow
  DELETE /api/cashflows/{id} delete a cash flow
  GET  /api/risk           current risk
  POST /api/import/transactions  import Avanza CSV (?preview=true to parse only)
  POST /api/fx/refresh     refresh FX rates (Riksbank/ECB)
  POST /api/prices/refresh refresh prices from MinIO
  GET  /                   serves the static dashboard

Run:  python -m alphaos.cli serve   (or:  uvicorn alphaos.server:app --port 8503)
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import session_scope, have_database, db_status
from .db import config as dbconfig
from .db import allocation as dballoc
from .db import nav as dbnav
from .db import history as dbhistory
from .db import fx as dbfx, pricing as dbpricing, importer as dbimporter
from .db import transactions as dbtx
from .db import cash_flows as dbcf
from .db.serialize import (
    jsonable, sleeve_to_dict, holding_to_dict, nav_snapshot_to_dict,
    config_to_dict, transaction_to_dict, cash_flow_to_dict,
)

_DB_UNCONFIGURED = JSONResponse(status_code=503, content={"error": "database not configured"})

try:
    from . import __version__ as APP_VERSION
except Exception:  # pragma: no cover
    APP_VERSION = "unknown"

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="AlphaOS — V2-FRONTIER dashboard")

app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


# --- Health / status ---

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def status() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "version": APP_VERSION,
        "database": db_status(),
    })


# --- Config ---

@app.get("/api/config")
def get_config() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        cfg = dbconfig.get_config(session)
        return JSONResponse({"config": config_to_dict(cfg)})


@app.post("/api/config")
async def post_config(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            cfg = dbconfig.update_config(session, **body)
            payload = {"config": config_to_dict(cfg)}
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# --- Sleeves ---

@app.get("/api/sleeves")
def get_sleeves() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        sleeves = dballoc.list_sleeves(session)
        return JSONResponse({"sleeves": [sleeve_to_dict(s) for s in sleeves]})


@app.post("/api/sleeves")
async def post_sleeve(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            sleeve = dballoc.upsert_sleeve(
                session,
                body["code"],
                name=body.get("name"),
                kind=body.get("kind"),
                target_weight=body.get("target_weight"),
                sort_order=body.get("sort_order"),
                notes=body.get("notes"),
            )
            payload = {"sleeve": sleeve_to_dict(sleeve)}
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# --- Holdings ---

@app.get("/api/holdings")
def get_holdings() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        holdings = dballoc.list_holdings(session)
        return JSONResponse({"holdings": [holding_to_dict(h) for h in holdings]})


@app.post("/api/holdings")
async def post_holding(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            holding = dballoc.upsert_holding(
                session,
                id=body.get("id"),
                sleeve_code=body.get("sleeve_code"),
                sleeve_id=body.get("sleeve_id"),
                symbol=body.get("symbol"),
                isin=body.get("isin"),
                name=body.get("name"),
                asset_class=body.get("asset_class"),
                currency=body.get("currency"),
                quantity=body.get("quantity"),
                avg_price=body.get("avg_price"),
                cost_basis_sek=body.get("cost_basis_sek"),
                last_price=body.get("last_price"),
                last_price_date=body.get("last_price_date"),
                price_source=body.get("price_source"),
                acquired_at=body.get("acquired_at"),
                as_of=body.get("as_of"),
                notes=body.get("notes"),
            )
            payload = {"holding": holding_to_dict(holding)}
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/holdings/{holding_id}")
def delete_holding(holding_id: int) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        deleted = dballoc.delete_holding(session, holding_id)
        return JSONResponse({"deleted": deleted})


# --- Allocation ---

@app.get("/api/allocation")
def get_allocation() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse(jsonable(dballoc.allocation(session)))


# --- NAV / risk ---

@app.get("/api/nav")
def get_nav() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        snapshots = dbnav.list_snapshots(session)
        risk = dbnav.current_risk(session)
        rows = [nav_snapshot_to_dict(n) for n in snapshots]
        # Until the daily job has accumulated history, reconstruct the past from
        # the transaction ledger + historical closes so the chart has a curve.
        reconstructed = False
        if len(rows) < 2:
            series = dbhistory.reconstruct_series(session)
            if len(series) >= 2:
                rows = series
                reconstructed = True
        return JSONResponse({
            "snapshots": rows,
            "reconstructed": reconstructed,
            "risk": jsonable(risk),
        })


@app.post("/api/nav")
async def post_nav(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            snapshot = dbnav.add_snapshot(
                session,
                as_of=body["as_of"],
                gross_asset_value=body.get("gross_asset_value"),
                loan_balance=body.get("loan_balance"),
                net_contribution=body.get("net_contribution"),
                notes=body.get("notes"),
            )
            payload = {"snapshot": nav_snapshot_to_dict(snapshot)}
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/nav/snapshot-now")
async def post_snapshot_now(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        with session_scope() as session:
            snap = dbnav.upsert_snapshot(
                session,
                as_of=body.get("as_of") or dt.date.today(),
                notes=body.get("notes"),
            )
            payload = {"snapshot": nav_snapshot_to_dict(snap)}
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/nav/backfill")
async def post_nav_backfill(request: Request) -> JSONResponse:
    """Reconstruct and PERSIST the daily NAV series from the transaction ledger +
    historical closes, replacing any snapshots in the covered range."""
    if not have_database():
        return _DB_UNCONFIGURED
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        with session_scope() as session:
            written = dbhistory.backfill_snapshots(
                session, start=body.get("start"), end=body.get("end")
            )
        return JSONResponse({"written": written})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/nav/journal")
def get_nav_journal(days: int = 365, sleeve_only: bool = True) -> JSONResponse:
    """Daily money journal (value/cost/day-P&L/events) for the heatmap + feed."""
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse({
            "journal": dbhistory.daily_journal(session, days=days, sleeve_only=sleeve_only),
            "sleeve_only": sleeve_only,
        })


@app.get("/api/nav/holdings-on")
def get_holdings_on(date: str, sleeve_only: bool = True) -> JSONResponse:
    """What was held on a given calendar date, with per-position valuation."""
    if not have_database():
        return _DB_UNCONFIGURED
    try:
        with session_scope() as session:
            rows = dbhistory.holdings_on(session, date, sleeve_only=sleeve_only)
        return JSONResponse({"date": date, "holdings": rows})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/risk")
def get_risk() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse({"risk": jsonable(dbnav.current_risk(session))})


# --- Import / FX / prices ---

@app.post("/api/import/transactions")
async def import_tx(file: UploadFile = File(...), preview: bool = False) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    content = await file.read()
    try:
        if preview:
            return JSONResponse(jsonable({
                "preview": True,
                **dbimporter.preview_import(content),
            }))
        with session_scope() as session:
            result = dbimporter.import_transactions(session, content)
            payload = jsonable({"summary": result})
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# --- Transactions / history ---

@app.get("/api/transactions")
def get_transactions(isin: str | None = None, limit: int | None = None) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        txns = dbtx.list_transactions(session, isin=isin, limit=limit)
        return JSONResponse({"transactions": [transaction_to_dict(t) for t in txns]})


@app.post("/api/transactions")
async def post_transaction(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            txn = dbtx.add_transaction(
                session,
                date=body["date"],
                isin=body["isin"],
                kind=body["kind"],
                quantity=body["quantity"],
                price=body["price"],
                currency=body.get("currency", "SEK"),
                amount_sek=body.get("amount_sek"),
                fees_sek=body.get("fees_sek", 0),
                symbol=body.get("symbol"),
                name=body.get("name"),
                note=body.get("note"),
            )
            payload = {"transaction": transaction_to_dict(txn)}
        return JSONResponse(payload)
    except (ValueError, KeyError) as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/transactions/{txn_id}")
def delete_transaction(txn_id: int) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse({"deleted": dbtx.delete_transaction(session, txn_id)})


@app.get("/api/history")
def get_history(isin: str) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse(jsonable({
            "isin": isin,
            "history": dbtx.position_history(session, isin),
        }))


@app.post("/api/fx/refresh")
def post_fx_refresh() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse(jsonable({"fx": dbfx.refresh_fx(session)}))


@app.post("/api/prices/refresh")
def post_prices_refresh() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse(jsonable({"prices": dbpricing.refresh_prices(session)}))


# --- Cash flows ---

@app.get("/api/cashflows")
def get_cashflows(limit: int | None = None) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        flows = dbcf.list_cash_flows(session, limit=limit)
        return JSONResponse({"cashflows": [cash_flow_to_dict(c) for c in flows]})


@app.post("/api/cashflows")
async def post_cashflow(request: Request) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    body = await request.json()
    try:
        with session_scope() as session:
            cf = dbcf.add_cash_flow(
                session,
                date=body["date"],
                amount_sek=body["amount_sek"],
                kind=body["kind"],
                note=body.get("note"),
            )
            payload = {"cashflow": cash_flow_to_dict(cf)}
        return JSONResponse(payload)
    except (ValueError, KeyError) as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/cashflows/{cf_id}")
def delete_cashflow(cf_id: int) -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse({"deleted": dbcf.delete_cash_flow(session, cf_id)})


# --- Static dashboard ---

@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/{page}.html")
def page(page: str) -> FileResponse:
    path = WEB_DIR / f"{page}.html"
    if not path.exists():
        return FileResponse(WEB_DIR / "index.html")
    return FileResponse(path)
