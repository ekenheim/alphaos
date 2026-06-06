"""FastAPI backend serving the AlphaOS dashboard.

Endpoints:
  GET /api/health          health probe
  GET /api/portfolio       META-EA strategy summary (KPIs + equity + monthly heatmap)
  GET /api/propfirm        prop-firm portfolio summary (10 sim accounts)
  GET /api/strategies      per-instrument strategy comparison
  GET /                    serves the static dashboard

Run:  python -m alphaos.cli serve   (or:  uvicorn alphaos.server:app --port 8503)
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Lock

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .portfolio import (
    PROP_FIRMS, STRATEGY_SYMBOLS, portfolio_summary_json,
    prop_portfolio_summary_json, run_portfolio, simulate_prop_portfolio,
)

WEB_DIR = Path(__file__).resolve().parent / "web"
# Screenshots live inside the package so alphaos is fully self-contained.
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"

app = FastAPI(title="AlphaOS — META-EA prop dashboard")

app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
if SCREENSHOTS_DIR.exists():
    app.mount("/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")


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


@app.get("/api/inspiration")
def inspiration() -> JSONResponse:
    """List screenshot files so the gallery can render them."""
    if not SCREENSHOTS_DIR.exists():
        return JSONResponse({"images": []})
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    files = sorted([
        f"/screenshots/{p.name}"
        for p in SCREENSHOTS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    ])
    return JSONResponse({"images": files})


@app.get("/api/paper")
def paper_endpoint() -> JSONResponse:
    """Forward-test paper ledger + summary stats."""
    from . import paper as zpaper
    s = zpaper.summary()
    ledger = zpaper.load_ledger()
    # Serialize last 25 rows newest-first
    rows = []
    if not ledger.empty:
        recent = ledger.sort_values("signal_ts", ascending=False).head(25)
        for _, r in recent.iterrows():
            rows.append({
                "signal_ts": r["signal_ts"].isoformat() if pd.notna(r["signal_ts"]) else None,
                "symbol": r["symbol"], "setup": r["setup"],
                "entry_px": float(r["entry_px"]) if pd.notna(r["entry_px"]) else None,
                "stop_px":  float(r["stop_px"])  if pd.notna(r["stop_px"])  else None,
                "target_px": float(r["target_px"]) if pd.notna(r["target_px"]) else None,
                "exit_px":  float(r["exit_px"])  if pd.notna(r["exit_px"])  else None,
                "exit_reason": str(r["exit_reason"]) if pd.notna(r["exit_reason"]) else "",
                "r_multiple": float(r["r_multiple"]) if pd.notna(r["r_multiple"]) else None,
                "status": str(r["status"]),
            })
    return JSONResponse({"summary": s, "ledger": rows})


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
