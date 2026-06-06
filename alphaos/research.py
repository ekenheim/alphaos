"""Research harness — slice + sweep tools for iterating on alphaos_ea.

Honest research mode: surface where the P&L actually lives, prove improvements
beat sticky-Markov PLACEBO before declaring victory. Kill ideas that don't.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from . import data as zdata
from . import levels as zlevels
from . import setups as zsetups
from .backtest import BacktestResult, run_backtest
from .portfolio import STRATEGY_SYMBOLS, run_portfolio


@dataclass
class Slice:
    bucket: str
    n_trades: int
    win_rate: float
    avg_r: float
    profit_factor: float
    total_r: float


def _profit_factor(rs: list[float]) -> float:
    gross_w = sum(r for r in rs if r > 0)
    gross_l = -sum(r for r in rs if r <= 0)
    return float(gross_w / gross_l) if gross_l > 0 else float("inf")


def _slice_stats(label: str, rs: list[float]) -> Slice:
    if not rs:
        return Slice(label, 0, 0.0, 0.0, 0.0, 0.0)
    arr = np.array(rs)
    return Slice(
        bucket=label,
        n_trades=len(arr),
        win_rate=float((arr > 0).mean()),
        avg_r=float(arr.mean()),
        profit_factor=_profit_factor(arr.tolist()),
        total_r=float(arr.sum()),
    )


def diagnose_runs(runs) -> dict[str, list[Slice]]:
    """Slice the trades by instrument, hour-of-day, day-of-week.

    Returns dict: {dim_name: [Slice]}. Use to surface lossy buckets to filter out.
    """
    rows = []
    for r in runs:
        for t in r.result.trades:
            rows.append({
                "symbol": r.symbol,
                "entry_ts": t.entry_ts,
                "r": t.r_multiple,
                "hour": pd.Timestamp(t.entry_ts).hour,
                "dow": pd.Timestamp(t.entry_ts).day_name()[:3],
            })
    if not rows:
        return {"symbol": [], "hour": [], "dow": []}
    df = pd.DataFrame(rows)

    out = {"symbol": [], "hour": [], "dow": []}
    for sym, g in df.groupby("symbol"):
        out["symbol"].append(_slice_stats(sym, g["r"].tolist()))
    for h, g in df.groupby("hour"):
        out["hour"].append(_slice_stats(f"{int(h):02d}:00 UTC", g["r"].tolist()))
    dow_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for d in dow_order:
        sub = df[df["dow"] == d]
        if len(sub):
            out["dow"].append(_slice_stats(d, sub["r"].tolist()))

    return out


def print_diagnostic(slices: dict[str, list[Slice]]) -> None:
    """Pretty-print slice tables in a way that's easy to scan."""
    for dim, items in slices.items():
        print(f"\n=== by {dim} ===")
        print(f"{'bucket':<14} {'n':>5} {'win%':>7} {'avg R':>8} {'PF':>6} {'tot R':>8}")
        for s in sorted(items, key=lambda x: x.total_r):
            wr = f"{s.win_rate * 100:5.1f}%"
            print(f"{s.bucket:<14} {s.n_trades:>5} {wr:>7} {s.avg_r:>+8.2f} {s.profit_factor:>6.2f} {s.total_r:>+8.2f}")


# ---------- Filter framework ----------

def time_filter(df: pd.DataFrame, allowed_hours: Iterable[int] | None = None,
                allowed_dows: Iterable[int] | None = None) -> pd.Series:
    """Boolean Series: True where the bar's timestamp is in the allowed buckets.

    allowed_hours: ints 0..23 (UTC). None = all hours.
    allowed_dows:  ints 0..6 (Mon..Sun). None = all days.
    """
    hours = df.index.hour
    dows = df.index.dayofweek
    mask = pd.Series(True, index=df.index)
    if allowed_hours is not None:
        mask &= pd.Series(np.isin(hours, list(allowed_hours)), index=df.index)
    if allowed_dows is not None:
        mask &= pd.Series(np.isin(dows, list(allowed_dows)), index=df.index)
    return mask


def trend_strength_filter(df: pd.DataFrame, lookback: int = 50, min_slope_pct: float = 0.05) -> pd.Series:
    """Require slow EMA to have risen by at least min_slope_pct over `lookback` bars."""
    ema = df["close"].ewm(span=lookback, adjust=False, min_periods=lookback).mean()
    slope = (ema - ema.shift(lookback)) / ema.shift(lookback)
    return slope > min_slope_pct


def run_with_filter(symbol: str, interval: str, setup: str,
                    filt: Callable[[pd.DataFrame], pd.Series] | None = None,
                    lookback_days: int = 700,
                    stop_atr: float = 1.0, target_atr: float = 6.0,
                    trail_atr: float = 1.5, trail_activate_atr: float = 2.0,
                    max_hold_bars: int = 120,
                    cost_bps: float = 1.0, slippage_frac: float = 0.05,
                    ) -> BacktestResult | None:
    """Run a single symbol with an optional filter ANDed onto the raw signal."""
    end = pd.Timestamp.utcnow().normalize()
    start = (end - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        df = zdata.fetch_ohlcv(symbol, interval=interval, start=start)
    except Exception:
        return None
    if len(df) < 200:
        return None
    df = zlevels.attach_all_levels(df)
    sig = zsetups.detect(setup, df)
    if filt is not None:
        sig = sig & filt(df).reindex(sig.index, fill_value=False).astype(bool)
    return run_backtest(
        df, sig,
        stop_atr=stop_atr, target_atr=target_atr,
        trail_atr=trail_atr, trail_activate_atr=trail_activate_atr,
        max_hold_bars=max_hold_bars,
        cost_bps=cost_bps, slippage_frac=slippage_frac,
    )


@dataclass
class FilterReport:
    name: str
    symbols: dict[str, BacktestResult]

    @property
    def total_trades(self) -> int:
        return sum(r.n_trades for r in self.symbols.values())

    @property
    def win_rate(self) -> float:
        trades = [t for r in self.symbols.values() for t in r.trades]
        if not trades:
            return 0.0
        return sum(1 for t in trades if t.r_multiple > 0) / len(trades)

    @property
    def profit_factor(self) -> float:
        rs = [t.r_multiple for r in self.symbols.values() for t in r.trades]
        return _profit_factor(rs)

    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for r in self.symbols.values() for t in r.trades)

    @property
    def sharpe(self) -> float:
        return float(np.mean([r.sharpe for r in self.symbols.values() if r.n_trades > 0]) or 0.0)


def evaluate_filters(filters: dict[str, Callable[[pd.DataFrame], pd.Series] | None],
                     symbols: Iterable[str] = STRATEGY_SYMBOLS,
                     interval: str = "1h",
                     setup: str = "alphaos_ea",
                     ) -> list[FilterReport]:
    """Run the same setup × symbols with each filter, return one report per filter."""
    reports = []
    for name, filt in filters.items():
        per_sym: dict[str, BacktestResult] = {}
        for sym in symbols:
            res = run_with_filter(sym, interval, setup, filt=filt)
            if res is not None:
                per_sym[sym] = res
        reports.append(FilterReport(name, per_sym))
    return reports


def walk_forward_filter_discovery(
    symbols: Iterable[str] = STRATEGY_SYMBOLS,
    interval: str = "1h",
    setup: str = "alphaos_ea",
    train_frac: float = 0.6,
    min_bucket_n: int = 8,
    bucket_pf_kill: float = 0.6,
    bucket_pf_keep: float = 1.0,
) -> dict:
    """Discover bad hour/dow buckets on TRAIN half, score on VAL half. No look-ahead.

    Returns dict with:
      - 'train_baseline', 'val_baseline'   : FilterReport-style stats with no filter
      - 'val_filtered'                     : Same but with the train-discovered filter applied
      - 'discovered'                       : the kept hours/dows / dropped symbols
      - 'verdict'                          : 'PASS' if val_filtered.PF > val_baseline.PF, else 'KILL'
    """
    train_rows = []
    val_rows = []
    per_symbol_pf_train: dict[str, float] = {}
    val_baseline_per_symbol: dict[str, BacktestResult] = {}

    for sym in symbols:
        end = pd.Timestamp.utcnow().normalize()
        start = (end - pd.Timedelta(days=700)).strftime("%Y-%m-%d")
        try:
            df = zdata.fetch_ohlcv(sym, interval=interval, start=start)
        except Exception:
            continue
        if len(df) < 400:
            continue
        df = zlevels.attach_all_levels(df)
        cut = int(len(df) * train_frac)
        df_train, df_val = df.iloc[:cut], df.iloc[cut:]

        sig_tr = zsetups.detect(setup, df_train)
        res_tr = run_backtest(df_train, sig_tr, stop_atr=1.0, target_atr=6.0,
                              trail_atr=1.5, trail_activate_atr=2.0, max_hold_bars=120)
        sig_va = zsetups.detect(setup, df_val)
        res_va = run_backtest(df_val, sig_va, stop_atr=1.0, target_atr=6.0,
                              trail_atr=1.5, trail_activate_atr=2.0, max_hold_bars=120)
        val_baseline_per_symbol[sym] = res_va

        if res_tr.n_trades > 0:
            r_list = [t.r_multiple for t in res_tr.trades]
            per_symbol_pf_train[sym] = _profit_factor(r_list)
        for t in res_tr.trades:
            train_rows.append({"symbol": sym, "hour": pd.Timestamp(t.entry_ts).hour,
                                "dow": pd.Timestamp(t.entry_ts).dayofweek, "r": t.r_multiple})
        for t in res_va.trades:
            val_rows.append({"symbol": sym, "hour": pd.Timestamp(t.entry_ts).hour,
                              "dow": pd.Timestamp(t.entry_ts).dayofweek, "r": t.r_multiple})

    if not train_rows:
        return {"verdict": "INCONCLUSIVE", "reason": "no train data"}

    df_tr = pd.DataFrame(train_rows)
    df_va = pd.DataFrame(val_rows)

    # ----- Discover bad/good buckets on TRAIN ONLY -----
    kept_hours = []
    for h, g in df_tr.groupby("hour"):
        if len(g) >= min_bucket_n:
            pf = _profit_factor(g["r"].tolist())
            if pf >= bucket_pf_kill:
                kept_hours.append(int(h))
        else:
            kept_hours.append(int(h))  # keep low-sample hours by default (don't overfit)

    kept_dows = []
    for d, g in df_tr.groupby("dow"):
        if len(g) >= min_bucket_n:
            pf = _profit_factor(g["r"].tolist())
            if pf >= bucket_pf_kill:
                kept_dows.append(int(d))
        else:
            kept_dows.append(int(d))

    dropped_symbols = [
        sym for sym, pf in per_symbol_pf_train.items()
        if pf < bucket_pf_kill and len([t for t in train_rows if t["symbol"] == sym]) >= min_bucket_n * 2
    ]

    # ----- Apply filter to VAL set -----
    def passes_filter(row: dict) -> bool:
        if row["symbol"] in dropped_symbols:
            return False
        if row["hour"] not in kept_hours:
            return False
        if row["dow"] not in kept_dows:
            return False
        return True

    val_filt_rs = [row["r"] for row in val_rows if passes_filter(row)]
    val_base_rs = [row["r"] for row in val_rows]

    def _summary(rs: list[float]) -> dict:
        if not rs:
            return {"n": 0, "win_rate": 0.0, "pf": 0.0, "total_r": 0.0, "avg_r": 0.0}
        arr = np.array(rs)
        return {
            "n": int(len(arr)),
            "win_rate": float((arr > 0).mean()),
            "pf": _profit_factor(arr.tolist()),
            "total_r": float(arr.sum()),
            "avg_r": float(arr.mean()),
        }

    train_baseline = _summary([row["r"] for row in train_rows])
    val_baseline = _summary(val_base_rs)
    val_filtered = _summary(val_filt_rs)

    improved_pf = val_filtered["pf"] > val_baseline["pf"]
    improved_total = val_filtered["total_r"] > val_baseline["total_r"]
    verdict = "PASS" if (improved_pf and improved_total) else "KILL"

    return {
        "train_baseline": train_baseline,
        "val_baseline":   val_baseline,
        "val_filtered":   val_filtered,
        "discovered": {
            "kept_hours": sorted(kept_hours),
            "kept_dows":  sorted(kept_dows),
            "dropped_symbols": dropped_symbols,
            "train_symbol_pf": per_symbol_pf_train,
        },
        "verdict": verdict,
        "improved_pf": improved_pf,
        "improved_total_r": improved_total,
    }


def print_filter_table(reports: list[FilterReport]) -> None:
    print(f"\n{'filter':<28} {'trades':>7} {'win%':>7} {'PF':>6} {'tot R':>8} {'avg Sh':>7}")
    print("-" * 72)
    base = next((r for r in reports if r.name == "baseline"), None)
    for r in reports:
        wr = f"{r.win_rate * 100:5.1f}%"
        delta = ""
        if base and r.name != "baseline":
            d = r.profit_factor - base.profit_factor
            delta = f" ({d:+.2f})"
        print(f"{r.name:<28} {r.total_trades:>7} {wr:>7} {r.profit_factor:>6.2f}{delta} {r.total_r:>+8.2f} {r.sharpe:>+7.2f}")
