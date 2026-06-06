"""Sticky-Markov PLACEBO baseline.

Per CLAUDE.md regime/timing discipline: any rule-based timing signal must beat
P95 of a placebo distribution generated from sticky binary timers with matched
in/out frequency. This module wires that up so any setup we ship has a defensible
"better than random at the same firing cadence" test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import BacktestResult, run_backtest


@dataclass
class PlaceboResult:
    real_sharpe: float
    placebo_sharpes: np.ndarray
    p95: float
    p99: float
    rank_pct: float  # percentile of real_sharpe vs placebo distribution
    passed: bool    # real_sharpe > p95


def _firing_stats(signals: pd.Series) -> tuple[float, float]:
    """Return (p_in, stickiness). p_in = P(signal=True), stickiness = P(stay | in)."""
    s = signals.fillna(False).astype(bool).to_numpy()
    p_in = float(s.mean())
    if s.sum() < 2:
        return p_in, 0.5
    # Stickiness: among True bars (except last), P(next is True)
    stays = ((s[:-1]) & (s[1:])).sum()
    in_count = s[:-1].sum()
    stickiness = float(stays / in_count) if in_count > 0 else 0.5
    return p_in, stickiness


def _sticky_markov_series(n: int, p_in: float, stickiness: float, rng: np.random.Generator) -> np.ndarray:
    """Two-state Markov chain matched to (p_in, stickiness)."""
    if p_in <= 0 or p_in >= 1:
        return np.zeros(n, dtype=bool)
    # From p_in = pi_in and stickiness = P(in|in) = p_ii, derive P(in|out) = p_oi
    # Stationary: pi_in = p_oi / (p_oi + 1 - p_ii) -> p_oi = pi_in * (1 - p_ii) / (1 - pi_in)
    p_ii = float(np.clip(stickiness, 0.01, 0.99))
    p_oi = float(np.clip(p_in * (1 - p_ii) / max(1 - p_in, 1e-6), 0.001, 0.5))
    out = np.zeros(n, dtype=bool)
    state = rng.random() < p_in
    for i in range(n):
        out[i] = state
        p_stay = p_ii if state else (1 - p_oi)
        state = (rng.random() < (1 - p_stay)) ^ state  # flip with prob (1 - p_stay)
    return out


def run_placebo(
    df: pd.DataFrame,
    signals: pd.Series,
    n_runs: int = 200,
    seed: int = 42,
    **backtest_kwargs,
) -> PlaceboResult:
    """Run the same backtest with sticky-Markov random signals matched to the real
    signal's firing rate and autocorrelation."""
    rng = np.random.default_rng(seed)
    p_in, stickiness = _firing_stats(signals)

    real = run_backtest(df, signals, **backtest_kwargs)

    placebo_sharpes: list[float] = []
    for _ in range(n_runs):
        fake = _sticky_markov_series(len(df), p_in, stickiness, rng)
        fake_s = pd.Series(fake, index=df.index)
        res = run_backtest(df, fake_s, **backtest_kwargs)
        placebo_sharpes.append(res.sharpe)

    arr = np.array(placebo_sharpes)
    p95 = float(np.percentile(arr, 95))
    p99 = float(np.percentile(arr, 99))
    rank = float((arr < real.sharpe).mean())
    return PlaceboResult(
        real_sharpe=real.sharpe,
        placebo_sharpes=arr,
        p95=p95,
        p99=p99,
        rank_pct=rank,
        passed=real.sharpe > p95,
    )
