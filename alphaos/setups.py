"""Setup detectors. Each returns a boolean Series of entry signals (True at the bar
the signal triggers). All signals are causal: at bar t they reference only data
available at or before t. The backtest enters at next bar open.

Four canonical setups (the levels-trader common denominator):
- orb_break:               Opening-range high break on volume — long-only here.
- pdh_reclaim:             Pullback under PDH, then reclaim with momentum.
- vwap_reclaim:            Reclaim of session VWAP from below.
- momentum_continuation:   Higher-high after a clean pullback inside trend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

SetupFn = Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True)
class SetupSpec:
    name: str
    description: str
    detect: SetupFn


def _safe_bool(s: pd.Series) -> pd.Series:
    return s.fillna(False).astype(bool)


def detect_orb_break(df: pd.DataFrame, vol_lookback: int = 20, vol_mult: float = 1.3) -> pd.Series:
    """Long entry on the first bar that closes above ORH with volume > vol_mult x rolling mean."""
    close = df["close"]
    orh = df["orh"]
    vol = df["volume"]
    vol_ref = vol.rolling(vol_lookback, min_periods=5).mean().shift(1)  # shift -> causal

    breaking = (close > orh) & orh.notna()
    not_already_broken = ~(close.shift(1) > orh)
    vol_ok = vol > (vol_mult * vol_ref)
    return _safe_bool(breaking & not_already_broken & vol_ok)


def detect_pdh_reclaim(df: pd.DataFrame, dip_bars: int = 6) -> pd.Series:
    """Price dipped below PDH within the last `dip_bars`, now reclaims (close > PDH)."""
    close = df["close"]
    pdh = df["pdh"]
    dipped = (close.shift(1).rolling(dip_bars, min_periods=1).min() < pdh)
    reclaim = (close > pdh) & (close.shift(1) <= pdh)
    return _safe_bool(reclaim & dipped & pdh.notna())


def detect_vwap_reclaim(df: pd.DataFrame, trend_lookback: int = 12) -> pd.Series:
    """Reclaim VWAP from below; PDH not yet tagged today (early-trend filter)."""
    close = df["close"]
    vwap = df["vwap"]
    pdh = df["pdh"]
    hod = df["hod"]

    cross_up = (close > vwap) & (close.shift(1) <= vwap)
    was_below = (close.shift(1).rolling(trend_lookback, min_periods=2).min() < vwap)
    pdh_not_yet = (hod <= pdh) | pdh.isna()
    return _safe_bool(cross_up & was_below & pdh_not_yet & vwap.notna())


def detect_momentum_continuation(df: pd.DataFrame, pull_bars: int = 8, hi_bars: int = 20) -> pd.Series:
    """Higher-high after a pullback that did not break the running session low."""
    close = df["close"]
    high = df["high"]
    lod = df["lod"]
    recent_hi = high.shift(1).rolling(hi_bars, min_periods=hi_bars).max()
    pull_low = close.shift(1).rolling(pull_bars, min_periods=pull_bars).min()

    new_high = close > recent_hi
    held_structure = pull_low > lod  # pullback stayed above session low
    return _safe_bool(new_high & held_structure & lod.notna())


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def detect_alphaos_ea(df: pd.DataFrame,
                    fast: int = 20, slow: int = 50, trend: int = 200,
                    hi_bars: int = 50, vol_ratio: float = 1.10,
                    cooldown_bars: int = 24) -> pd.Series:
    """Volatility-regime-gated trend breakout — the EA-style template.

    Long entry when ALL hold:
      - Trend up: close > EMA(fast) > EMA(slow) > EMA(trend)
      - Volatility expanding: ATR(14) > vol_ratio * ATR(50)
      - Breakout: close > rolling N-bar high (close.shift(1)-based, so causal)
      - Not extended: prior bar was NOT also a new high (one-shot at first break)
      - Cooldown: no entry within `cooldown_bars` of last entry (sparse signal)

    Pairs with target_atr=4 + trail_atr=1 in the backtest -> high RRR, low win-rate.
    """
    close = df["close"]
    high = df["high"]
    ema_f = _ema(close, fast)
    ema_s = _ema(close, slow)
    ema_t = _ema(close, trend)
    atr_short = _atr(df, 14)
    atr_long = _atr(df, 50)

    trend_up = (close > ema_f) & (ema_f > ema_s) & (ema_s > ema_t)
    vol_on   = atr_short > vol_ratio * atr_long
    n_bar_hi = high.shift(1).rolling(hi_bars, min_periods=hi_bars).max()
    fresh_break = (close > n_bar_hi) & (close.shift(1) <= n_bar_hi)

    raw = _safe_bool(trend_up & vol_on & fresh_break)

    # Apply cooldown by walking the boolean array
    out = raw.copy()
    arr = out.to_numpy()
    last_idx = -10_000
    for i, v in enumerate(arr):
        if v:
            if i - last_idx < cooldown_bars:
                arr[i] = False
            else:
                last_idx = i
    return pd.Series(arr, index=df.index)


SETUPS: dict[str, SetupSpec] = {
    "orb_break": SetupSpec(
        "orb_break",
        "Opening-range high break with volume confirmation.",
        detect_orb_break,
    ),
    "pdh_reclaim": SetupSpec(
        "pdh_reclaim",
        "Prior-day high reclaim after a dip.",
        detect_pdh_reclaim,
    ),
    "vwap_reclaim": SetupSpec(
        "vwap_reclaim",
        "Session VWAP reclaim from below; PDH not yet tagged.",
        detect_vwap_reclaim,
    ),
    "momentum_continuation": SetupSpec(
        "momentum_continuation",
        "Higher-high after a contained pullback inside trend.",
        detect_momentum_continuation,
    ),
    "alphaos_ea": SetupSpec(
        "alphaos_ea",
        "EA-style: trend (3-EMA stack) + volatility regime + fresh breakout + cooldown.",
        detect_alphaos_ea,
    ),
}


def detect(setup_name: str, df: pd.DataFrame) -> pd.Series:
    if setup_name not in SETUPS:
        raise KeyError(f"Unknown setup '{setup_name}'. Known: {list(SETUPS)}")
    return SETUPS[setup_name].detect(df)
