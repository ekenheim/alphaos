"""Vectorized event-driven backtest with realistic cost model.

Discipline:
- Signals trigger at bar t; entry fills at bar t+1 open. No same-bar fill.
- Stop & target sized in ATR; exits checked intrabar (pessimistic: if both stop
  and target hit in same bar, assume stop fills first).
- Costs: fixed bps + slippage proportional to bar range.
- Equity is bar-marked at close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd


Side = Literal["long", "short"]


@dataclass
class Trade:
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    side: Side
    entry: float
    exit: float
    stop: float
    target: float
    r_multiple: float
    pnl_pct: float
    exit_reason: str


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: pd.Series  # close-to-close equity index, base 1.0
    signals: pd.Series  # entry signals as bool series
    params: dict = field(default_factory=dict)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl_pct > 0) / len(self.trades)

    @property
    def avg_r(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.r_multiple for t in self.trades]))

    @property
    def cagr(self) -> float:
        if len(self.equity) < 2:
            return 0.0
        years = (self.equity.index[-1] - self.equity.index[0]).total_seconds() / (365.25 * 86400)
        if years <= 0:
            return 0.0
        return float(self.equity.iloc[-1] ** (1 / years) - 1)

    @property
    def sharpe(self) -> float:
        # Use log returns to avoid pct_change blow-up when equity approaches zero.
        # Clip equity to a small positive floor so log doesn't go to -inf.
        eq = self.equity.clip(lower=1e-6)
        rets = np.log(eq / eq.shift(1)).replace([np.inf, -np.inf], np.nan).dropna()
        if rets.std() == 0 or len(rets) < 2:
            return 0.0
        sec_per_bar = (self.equity.index[-1] - self.equity.index[0]).total_seconds() / max(len(self.equity) - 1, 1)
        bars_per_year = (365.25 * 86400) / max(sec_per_bar, 1)
        return float(rets.mean() / rets.std() * np.sqrt(bars_per_year))

    @property
    def max_dd(self) -> float:
        if len(self.equity) < 2:
            return 0.0
        peak = self.equity.cummax()
        dd = (self.equity / peak) - 1
        return float(dd.min())


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            (df["high"] - df["low"]),
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def run_backtest(
    df: pd.DataFrame,
    signals: pd.Series,
    side: Side = "long",
    stop_atr: float = 1.0,
    target_atr: float = 2.0,
    max_hold_bars: int = 78,  # ~1 RTH day on 5m
    cost_bps: float = 1.0,
    slippage_frac: float = 0.05,  # 5% of bar range
    risk_per_trade: float = 0.01,  # 1% equity at risk per trade
    trail_atr: float | None = None,
    trail_activate_atr: float = 1.0,
    eod_flat: bool = False,
    session_hours_utc: tuple[float, float] = (13.5, 20.0),  # US RTH default
    no_entry_after_utc: float | None = None,
) -> BacktestResult:
    """Bar-by-bar walk. Vectorized signal detection, sequential trade management."""
    df = df.copy()
    df["atr"] = _atr(df, n=14)

    trades: list[Trade] = []
    equity = pd.Series(1.0, index=df.index, dtype=float)
    eq = 1.0
    in_pos = False
    entry_ts = None
    entry_px = stop_px = target_px = bar_risk = 0.0
    entry_idx = 0

    sig_arr = signals.reindex(df.index, fill_value=False).to_numpy()
    open_a = df["open"].to_numpy()
    high_a = df["high"].to_numpy()
    low_a = df["low"].to_numpy()
    close_a = df["close"].to_numpy()
    atr_a = df["atr"].to_numpy()
    ts_arr = df.index.to_numpy()
    rng_a = high_a - low_a
    running_extreme = 0.0  # running max (long) or min (short) since entry
    atr_at_entry = 0.0

    # Session metadata: per-bar UTC hour-of-day + per-bar trading-session date
    ts_idx = df.index
    hours = ts_idx.hour + ts_idx.minute / 60.0
    sess_dates = ts_idx.tz_convert("America/New_York").normalize().date if eod_flat else None
    hours_a = hours.to_numpy()
    in_session = (hours_a >= session_hours_utc[0]) & (hours_a < session_hours_utc[1])
    if no_entry_after_utc is not None:
        entry_window = in_session & (hours_a < no_entry_after_utc)
    else:
        entry_window = in_session
    # Last RTH bar per session, for EOD-flat exit
    eod_bar_mask = None
    if eod_flat:
        # The last bar where in_session is True per NY date
        eod_bar_mask = pd.Series(False, index=df.index)
        sess_series = pd.Series([d.isoformat() if d else None for d in sess_dates], index=df.index)
        rth_only = pd.Series(in_session, index=df.index)
        for sess, g in pd.DataFrame({"in": rth_only, "sess": sess_series}).groupby("sess"):
            sub = g[g["in"]]
            if not sub.empty:
                eod_bar_mask.loc[sub.index[-1]] = True
        eod_bar_mask = eod_bar_mask.to_numpy()

    for i in range(len(df) - 1):
        if not in_pos and sig_arr[i] and not np.isnan(atr_a[i]) and entry_window[i]:
            # Fill at next bar open + slippage
            raw_fill = open_a[i + 1]
            slip = slippage_frac * rng_a[i + 1] if not np.isnan(rng_a[i + 1]) else 0.0
            entry_px = raw_fill + slip if side == "long" else raw_fill - slip
            entry_px *= (1 + cost_bps / 10_000) if side == "long" else (1 - cost_bps / 10_000)

            atr_at_entry = atr_a[i]
            if side == "long":
                stop_px = entry_px - stop_atr * atr_at_entry
                target_px = entry_px + target_atr * atr_at_entry
            else:
                stop_px = entry_px + stop_atr * atr_at_entry
                target_px = entry_px - target_atr * atr_at_entry

            bar_risk = abs(entry_px - stop_px)
            in_pos = True
            entry_ts = ts_arr[i + 1]
            entry_idx = i + 1
            running_extreme = entry_px
            continue

        if in_pos:
            held = i - entry_idx
            exit_reason = ""
            exit_px = np.nan

            # Update running extreme (used for trailing stop)
            if side == "long":
                running_extreme = max(running_extreme, high_a[i])
            else:
                running_extreme = min(running_extreme, low_a[i])

            # Trailing stop ratchet
            if trail_atr is not None:
                if side == "long":
                    advance = (running_extreme - entry_px) / max(atr_at_entry, 1e-9)
                    if advance >= trail_activate_atr:
                        trail_px = running_extreme - trail_atr * atr_at_entry
                        if trail_px > stop_px:
                            stop_px = trail_px
                else:
                    advance = (entry_px - running_extreme) / max(atr_at_entry, 1e-9)
                    if advance >= trail_activate_atr:
                        trail_px = running_extreme + trail_atr * atr_at_entry
                        if trail_px < stop_px:
                            stop_px = trail_px

            # Pessimistic intrabar order: stop fills before target if both touched.
            if side == "long":
                stop_hit = low_a[i] <= stop_px
                tgt_hit = high_a[i] >= target_px
            else:
                stop_hit = high_a[i] >= stop_px
                tgt_hit = low_a[i] <= target_px

            if stop_hit:
                exit_px = stop_px
                exit_reason = "stop"
            elif tgt_hit:
                exit_px = target_px
                exit_reason = "target"
            elif eod_flat and eod_bar_mask is not None and eod_bar_mask[i]:
                exit_px = close_a[i]
                exit_reason = "eod"
            elif held >= max_hold_bars:
                exit_px = close_a[i]
                exit_reason = "time"

            if exit_reason:
                slip = slippage_frac * rng_a[i] if not np.isnan(rng_a[i]) else 0.0
                exit_px = exit_px - slip if side == "long" else exit_px + slip
                exit_px *= (1 - cost_bps / 10_000) if side == "long" else (1 + cost_bps / 10_000)

                pnl_pct = (exit_px / entry_px - 1) if side == "long" else (entry_px / exit_px - 1)
                r_mult = (entry_px - stop_px and (pnl_pct * entry_px) / bar_risk) or 0.0
                eq *= 1 + risk_per_trade * r_mult
                trades.append(
                    Trade(
                        entry_ts=pd.Timestamp(entry_ts),
                        exit_ts=pd.Timestamp(ts_arr[i]),
                        side=side,
                        entry=entry_px,
                        exit=exit_px,
                        stop=stop_px,
                        target=target_px,
                        r_multiple=float(r_mult),
                        pnl_pct=float(pnl_pct),
                        exit_reason=exit_reason,
                    )
                )
                in_pos = False

        equity.iloc[i] = eq

    equity.iloc[-1] = eq
    return BacktestResult(
        trades=trades,
        equity=equity,
        signals=signals,
        params=dict(
            side=side,
            stop_atr=stop_atr,
            target_atr=target_atr,
            max_hold_bars=max_hold_bars,
            cost_bps=cost_bps,
            slippage_frac=slippage_frac,
            risk_per_trade=risk_per_trade,
        ),
    )


def walk_forward_split(df: pd.DataFrame, train_frac: float = 0.6) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simple time-split for train/val. No shuffling — strictly causal."""
    n = len(df)
    cut = int(n * train_frac)
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()
