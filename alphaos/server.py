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
  GET  /api/risk           current risk
  GET  /                   serves the static dashboard

Run:  python -m alphaos.cli serve   (or:  uvicorn alphaos.server:app --port 8503)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import session_scope, have_database, db_status
from .db import config as dbconfig
from .db import allocation as dballoc
from .db import nav as dbnav
from .db.serialize import (
    jsonable, sleeve_to_dict, holding_to_dict, nav_snapshot_to_dict,
    config_to_dict,
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
                symbol=body["symbol"],
                isin=body.get("isin"),
                name=body.get("name"),
                asset_class=body.get("asset_class"),
                currency=body.get("currency", "SEK"),
                quantity=body.get("quantity", 0),
                market_value=body.get("market_value", 0),
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
        return JSONResponse({
            "snapshots": [nav_snapshot_to_dict(n) for n in snapshots],
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
                loan_balance=body.get("loan_balance", 0),
                net_contribution=body.get("net_contribution", 0),
                notes=body.get("notes"),
            )
            payload = {"snapshot": nav_snapshot_to_dict(snapshot)}
        return JSONResponse(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/risk")
def get_risk() -> JSONResponse:
    if not have_database():
        return _DB_UNCONFIGURED
    with session_scope() as session:
        return JSONResponse({"risk": jsonable(dbnav.current_risk(session))})


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
