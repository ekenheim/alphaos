"""Paper-trade ledger — append-only journal of live signals + their fates.

Workflow:
  1. `scan_today()` runs the strategy on intraday data up to "now" and appends
     any fresh signals as `open` rows.
  2. `mark_to_market()` checks open positions against the latest bar; closes
     when stop, target, EOD, or max-hold conditions trigger.
  3. `summary()` reports forward-test stats: trades, win rate, PF, R, equity.

Storage: one parquet at alphaos/data_cache/paper_ledger.parquet. Append-only;
never mutates closed rows. The dashboard reads this for /paper.html.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from . import data as zdata
from . import levels as zlevels
from . import setups as zsetups
from . import minio as zminio
from .research import _profit_factor


LEDGER_PATH = Path(__file__).resolve().parent / "data_cache" / "paper_ledger.parquet"
LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)


# Canonical paper-trading config — pure intraday, EOD-flat, IBKR-ish costs.
# `side` per setup: "long" or "short". Discoverable per-setup via research.
DEFAULT_CONFIG = dict(
    interval="5min",
    setups=("orb_break", "vwap_reclaim"),
    setup_sides={"orb_break": "long", "vwap_reclaim": "long"},  # overridden if research finds fade-edge
    symbols=("US100", "XAUUSD", "JP225", "US30"),
    stock_symbols=("NVDA", "TSLA", "MU", "AMD", "PLTR"),  # appended when ALPHAOS_INCLUDE_STOCKS=1
    stop_atr=1.0,
    target_atr=3.0,
    max_hold_bars=78,           # one RTH session on 5m
    session_hours_utc=(13.5, 20.0),  # 09:30-16:00 ET
    no_entry_after_utc=18.5,         # no fresh entries last 90min
    cost_bps=0.5,
    slippage_frac=0.10,
)


def _empty_ledger() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "signal_ts", "symbol", "setup", "side",
        "entry_ts", "entry_px", "stop_px", "target_px", "atr_at_entry",
        "exit_ts", "exit_px", "exit_reason", "r_multiple",
        "status",
    ])


def load_ledger() -> pd.DataFrame:
    if LEDGER_PATH.exists():
        df = pd.read_parquet(LEDGER_PATH)
        for col in ("signal_ts", "entry_ts", "exit_ts"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
        return df
    return _empty_ledger()


def save_ledger(df: pd.DataFrame) -> None:
    df.to_parquet(LEDGER_PATH)


def _fetch_bars(symbol: str, interval: str, start: date, end: date) -> pd.DataFrame | None:
    """Try MinIO first (longer history); fall back to yfinance."""
    if zminio.have_credentials():
        df = zminio.fetch_resampled(symbol, target_tf=interval, source_tf="5min",
                                     start=start, end=end)
        if df is not None and not df.empty:
            return df
    try:
        return zdata.fetch_ohlcv(symbol, interval=interval, start=str(start), end=str(end))
    except Exception:
        return None


def scan_today(asof: datetime | None = None, config: dict | None = None) -> pd.DataFrame:
    """Scan a single RTH day, append fresh signals to the ledger. Returns new rows.

    `asof` defaults to now — pass a specific UTC datetime for replay/testing.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    asof = asof or datetime.now(timezone.utc)
    asof_pd = pd.Timestamp(asof).tz_convert("UTC")
    end_date = asof_pd.date()
    start_date = end_date - pd.Timedelta(days=10).to_pytimedelta()  # enough lookback for levels/ATR

    ledger = load_ledger()
    new_rows: list[dict] = []

    for symbol in cfg["symbols"]:
        df = _fetch_bars(symbol, cfg["interval"], start_date, end_date)
        if df is None or df.empty:
            continue
        df = df.loc[df.index <= asof_pd]
        if len(df) < 50:
            continue
        df = zlevels.attach_all_levels(df, or_minutes=15)
        from .backtest import _atr
        df["atr"] = _atr(df, n=14)

        for setup_name in cfg["setups"]:
            try:
                sig = zsetups.detect(setup_name, df)
            except Exception:
                continue
            # Only consider signals AT the current bar (asof) — no replay-back-fill
            current_bar = df.index[-1]
            if not bool(sig.loc[current_bar]):
                continue
            # Skip if we already logged this signal in the ledger
            if not ledger.empty and (
                (ledger["symbol"] == symbol) &
                (ledger["setup"] == setup_name) &
                (ledger["signal_ts"] == current_bar)
            ).any():
                continue

            entry_px = float(df.loc[current_bar, "close"])  # fill at this bar's close, conservative
            atr_now = float(df.loc[current_bar, "atr"])
            side = cfg.get("setup_sides", {}).get(setup_name, "long")
            if side == "long":
                stop_px = entry_px - cfg["stop_atr"] * atr_now
                target_px = entry_px + cfg["target_atr"] * atr_now
            else:
                stop_px = entry_px + cfg["stop_atr"] * atr_now
                target_px = entry_px - cfg["target_atr"] * atr_now
            new_rows.append(dict(
                signal_ts=current_bar,
                symbol=symbol,
                setup=setup_name,
                side=side,
                entry_ts=current_bar,
                entry_px=entry_px,
                stop_px=stop_px,
                target_px=target_px,
                atr_at_entry=atr_now,
                exit_ts=pd.NaT, exit_px=np.nan, exit_reason="",
                r_multiple=np.nan,
                status="open",
            ))

    if new_rows:
        out = pd.DataFrame(new_rows)
        ledger = pd.concat([ledger, out], axis=0, ignore_index=True)
        save_ledger(ledger)
        return out
    return _empty_ledger()


def mark_to_market(asof: datetime | None = None, config: dict | None = None) -> int:
    """Walk all open positions; close any that triggered stop/target/EOD.

    Returns number of positions closed.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    asof = asof or datetime.now(timezone.utc)
    asof_pd = pd.Timestamp(asof).tz_convert("UTC")

    ledger = load_ledger()
    if ledger.empty:
        return 0
    open_mask = ledger["status"] == "open"
    if not open_mask.any():
        return 0

    sess_end_h = cfg["session_hours_utc"][1]
    closed = 0
    for i in ledger.index[open_mask]:
        row = ledger.loc[i]
        symbol = row["symbol"]
        df = _fetch_bars(symbol, cfg["interval"],
                          row["entry_ts"].date(),
                          asof_pd.date() + pd.Timedelta(days=1).to_pytimedelta())
        if df is None or df.empty:
            continue
        # Bars after entry, up to asof
        after = df.loc[(df.index > row["entry_ts"]) & (df.index <= asof_pd)]
        if after.empty:
            continue

        stop_px = float(row["stop_px"])
        target_px = float(row["target_px"])
        bar_risk = abs(row["entry_px"] - stop_px)

        exit_ts = None
        exit_px = None
        exit_reason = ""
        side = row.get("side", "long") if hasattr(row, "get") else "long"
        for ts, bar in after.iterrows():
            if side == "long":
                if bar["low"] <= stop_px:
                    exit_ts, exit_px, exit_reason = ts, stop_px, "stop"; break
                if bar["high"] >= target_px:
                    exit_ts, exit_px, exit_reason = ts, target_px, "target"; break
            else:
                if bar["high"] >= stop_px:
                    exit_ts, exit_px, exit_reason = ts, stop_px, "stop"; break
                if bar["low"] <= target_px:
                    exit_ts, exit_px, exit_reason = ts, target_px, "target"; break
            hour = ts.hour + ts.minute / 60.0
            if hour >= sess_end_h and ts.date() == row["entry_ts"].date():
                exit_ts, exit_px, exit_reason = ts, float(bar["close"]), "eod"; break

        if exit_ts is None:
            # Time-stop catch
            held = len(after)
            if held >= cfg["max_hold_bars"]:
                last = after.iloc[-1]
                exit_ts, exit_px, exit_reason = after.index[-1], float(last["close"]), "time"

        if exit_ts is not None:
            raw = (exit_px - row["entry_px"]) / bar_risk if bar_risk > 0 else 0.0
            r_mult = raw if side == "long" else -raw
            ledger.loc[i, "exit_ts"] = exit_ts
            ledger.loc[i, "exit_px"] = exit_px
            ledger.loc[i, "exit_reason"] = exit_reason
            ledger.loc[i, "r_multiple"] = r_mult
            ledger.loc[i, "status"] = "closed"
            closed += 1

    if closed:
        save_ledger(ledger)
    return closed


def summary() -> dict:
    """Forward-test stats from the closed positions in the ledger."""
    ledger = load_ledger()
    closed = ledger[ledger["status"] == "closed"]
    open_n = int((ledger["status"] == "open").sum())
    if closed.empty:
        return dict(closed=0, open=open_n, win_rate=0.0, pf=0.0,
                    avg_r=0.0, total_r=0.0, cum_equity=1.0, by_symbol={}, by_setup={})
    rs = closed["r_multiple"].astype(float).tolist()
    eq = 1.0
    for r in rs:
        eq *= (1 + 0.01 * r)
    by_symbol = closed.groupby("symbol")["r_multiple"].agg(["count", "sum", "mean"]).to_dict("index")
    by_setup = closed.groupby("setup")["r_multiple"].agg(["count", "sum", "mean"]).to_dict("index")
    return dict(
        closed=int(len(closed)),
        open=open_n,
        win_rate=float((np.array(rs) > 0).mean()),
        pf=_profit_factor(rs),
        avg_r=float(np.mean(rs)),
        total_r=float(np.sum(rs)),
        cum_equity=float(eq),
        by_symbol={k: {kk: float(vv) for kk, vv in v.items()} for k, v in by_symbol.items()},
        by_setup={k: {kk: float(vv) for kk, vv in v.items()} for k, v in by_setup.items()},
    )
