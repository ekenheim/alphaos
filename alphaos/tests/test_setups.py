"""Sanity + causality tests. Run with:  pytest alphaos/tests -q"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from alphaos import levels as zlevels
from alphaos import setups as zsetups
from alphaos.backtest import run_backtest
from alphaos.placebo import _sticky_markov_series


def _make_synthetic_5m(n_days: int = 3, seed: int = 0) -> pd.DataFrame:
    """Synthetic 5m bars across n RTH sessions (US/Eastern 09:30-16:00)."""
    rng = np.random.default_rng(seed)
    rows = []
    start = pd.Timestamp("2024-06-03 13:30", tz="UTC")  # 09:30 NY
    for d in range(n_days):
        day0 = start + pd.Timedelta(days=d)
        for k in range(78):  # 78 x 5min = 390min = 6.5h RTH
            ts = day0 + pd.Timedelta(minutes=5 * k)
            base = 100 + d * 2 + k * 0.05 + rng.normal(0, 0.15)
            o = base + rng.normal(0, 0.05)
            c = base + rng.normal(0, 0.05)
            h = max(o, c) + abs(rng.normal(0, 0.1))
            lw = min(o, c) - abs(rng.normal(0, 0.1))
            v = int(rng.integers(1000, 5000))
            rows.append((ts, o, h, lw, c, v))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    df.index.name = "ts_utc"
    return df


def test_levels_are_causal_no_future_peeking():
    df = _make_synthetic_5m(n_days=4)
    out = zlevels.attach_all_levels(df, or_minutes=15)
    # First RTH day cannot know PDH (no prior session yet).
    first_day_mask = out.index.tz_convert("America/New_York").normalize() == out.index.tz_convert("America/New_York")[0].normalize()
    assert out.loc[first_day_mask, "pdh"].isna().all()
    # ORH is NaN before the OR window closes.
    # On day 1 the first OR-window of ORH should not yet be populated at t=0.
    assert pd.isna(out["orh"].iloc[0])


def test_orb_break_triggers_on_clear_breakout():
    """Force a clean breakout in the 4th day: push the run-up bars *below* ORH,
    then a single fresh break above with high volume on one specific bar."""
    df = _make_synthetic_5m(n_days=4, seed=1)
    out = zlevels.attach_all_levels(df, or_minutes=15)
    last = out.index[-30:]
    orh_val = out.loc[last, "orh"].max()
    # Suppress the pre-break window below ORH
    pre = last[:25]
    out.loc[pre, "close"] = orh_val - 1.0
    out.loc[pre, "high"] = orh_val - 0.5
    out.loc[pre, "low"] = orh_val - 1.5
    out.loc[pre, "volume"] = 1_000
    # Single fresh breakout bar
    brk = last[25]
    out.loc[brk, "close"] = orh_val + 2.0
    out.loc[brk, "high"] = orh_val + 2.5
    out.loc[brk, "volume"] = 50_000
    sig = zsetups.detect_orb_break(out)
    assert sig.loc[brk] == True
    assert sig.sum() >= 1


def test_backtest_no_same_bar_fill():
    """Entry timestamp must be strictly after signal timestamp (next bar open)."""
    df = _make_synthetic_5m(n_days=3, seed=2)
    df = zlevels.attach_all_levels(df, or_minutes=15)
    sig = pd.Series(False, index=df.index)
    if len(df) > 50:
        sig.iloc[40] = True
    res = run_backtest(df, sig, stop_atr=1.0, target_atr=2.0, cost_bps=0, slippage_frac=0)
    if res.trades:
        t = res.trades[0]
        assert t.entry_ts > df.index[40]


def test_zero_signals_zero_trades():
    df = _make_synthetic_5m(n_days=2)
    df = zlevels.attach_all_levels(df, or_minutes=15)
    sig = pd.Series(False, index=df.index)
    res = run_backtest(df, sig)
    assert res.n_trades == 0
    assert res.equity.iloc[-1] == pytest.approx(1.0)


def test_sticky_markov_matches_frequency_in_expectation():
    rng = np.random.default_rng(0)
    n = 10_000
    target_p = 0.05
    s = _sticky_markov_series(n, p_in=target_p, stickiness=0.7, rng=rng)
    realized = s.mean()
    # Loose bound — Markov mixing on n=10k with stickiness 0.7 still concentrates.
    assert 0.01 < realized < 0.12


def test_setups_all_run_without_error():
    df = _make_synthetic_5m(n_days=4)
    df = zlevels.attach_all_levels(df, or_minutes=15)
    for name in zsetups.SETUPS:
        sig = zsetups.detect(name, df)
        assert isinstance(sig, pd.Series)
        assert sig.dtype == bool
        assert len(sig) == len(df)
