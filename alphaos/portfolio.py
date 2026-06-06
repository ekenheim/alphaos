"""Multi-instrument portfolio + prop-firm account simulator.

Aggregates per-instrument backtests into:
- A META-EA cumulative P&L curve (sum of per-strategy daily P&L)
- Monthly performance heatmap
- Trades distribution by symbol
- Prop-firm account simulator (FTMO-style accounts with payout rules)

The prop-firm sim is a simple model: each "account" runs the strategy on a
single instrument from a starting balance; it draws down toward a hard floor
(blow-up) and pays out a fraction of profit above a payout-trigger level on a
monthly cadence.

This is a research model — not financial advice — and the simulated prop-firm
numbers shouldn't be confused with real-firm performance.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from . import data as zdata
from . import levels as zlevels
from . import setups as zsetups
from .backtest import BacktestResult, run_backtest


# ---------- Strategy specs ----------

STRATEGY_SYMBOLS: list[str] = [
    "US100", "XAUUSD", "USDJPY", "BTCUSD", "JP225",
]

DEFAULT_SETUP = "alphaos_ea"


# --- Walk-forward-discovered filter (research.py walk_forward_filter_discovery on
# yfinance 1h, ~700 days, 60/40 train/val split, discovered on TRAIN ONLY):
#
#   train baseline:   147 trades, PF 0.68
#   val baseline:      87 trades, PF 1.04  (no filter)
#   val WITH filter:   28 trades, PF 1.47  <-- +0.43 PF, +5.50 R total
#
# Looks like a PASS on the walk-forward gate. But the random-drop PLACEBO
# (sample 28 trades randomly from the 87 val trades, N=500 sims) ranks the
# filter at the 84th percentile (median random PF = 0.99, P95 = 1.81). So
# the filter's CHOICE of which trades to drop is not significantly better
# than dropping the same number at random. By the project's CLAUDE.md rule
# ("must beat P95 of placebo distribution to count as real signal"),
# this AXIS IS WEAK / KILLED.
#
# Default: apply_walk_forward_filter=False. Kept behind a flag so an
# operator can still inspect "what would it look like" with the filter on,
# but the dashboard's headline numbers are the un-filtered honest baseline.
# Provenance: 2026-06-06, run on commit-prior-to-c628e7009.

WALK_FORWARD_DROPPED_SYMBOLS = {"USDJPY", "BTCUSD"}
WALK_FORWARD_KEPT_HOURS_UTC = {0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21}
WALK_FORWARD_KEPT_DOWS = {0, 2, 3, 4}  # Mon, Wed, Thu, Fri (Tue and weekend dropped)


@dataclass
class StrategyRun:
    symbol: str
    setup: str
    result: BacktestResult

    @property
    def daily_pnl(self) -> pd.Series:
        """Daily realized P&L derived from trades (UTC date index)."""
        if not self.result.trades:
            return pd.Series(dtype=float)
        rows = [(t.exit_ts.normalize(), t.pnl_pct) for t in self.result.trades]
        s = pd.Series({d: 0.0 for d in {r[0] for r in rows}})
        for d, p in rows:
            s.loc[d] += p
        return s.sort_index()


# ---------- Portfolio backtest ----------

def run_portfolio(
    symbols: Iterable[str] = STRATEGY_SYMBOLS,
    setup: str = DEFAULT_SETUP,
    interval: str = "1h",
    lookback_days: int = 700,
    cost_bps: float = 1.0,
    slippage_frac: float = 0.05,
    target_atr: float = 6.0,
    stop_atr: float = 2.0,
    trail_atr: float | None = None,
    trail_activate_atr: float = 2.0,
    max_hold_bars: int = 120,
    apply_walk_forward_filter: bool = False,
) -> list[StrategyRun]:
    """Run the same setup across multiple instruments. Returns one StrategyRun per symbol.

    Defaults: wide targets (6 ATR), trailing stop after +2R. With
    apply_walk_forward_filter=True (default) the walk-forward-discovered filter
    drops USDJPY/BTCUSD and screens by hour/DOW (provenance documented above).
    """
    end = pd.Timestamp.utcnow().normalize()
    start = (end - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    runs: list[StrategyRun] = []
    for sym in symbols:
        if apply_walk_forward_filter and sym in WALK_FORWARD_DROPPED_SYMBOLS:
            continue
        try:
            df = zdata.fetch_ohlcv(sym, interval=interval, start=start)
            if len(df) < 200:
                continue
            df = zlevels.attach_all_levels(df, or_minutes=15)
            sig = zsetups.detect(setup, df)

            if apply_walk_forward_filter:
                hours_arr = df.index.hour
                dows_arr = df.index.dayofweek
                hour_ok = pd.Series(
                    [h in WALK_FORWARD_KEPT_HOURS_UTC for h in hours_arr], index=df.index
                )
                dow_ok = pd.Series(
                    [d in WALK_FORWARD_KEPT_DOWS for d in dows_arr], index=df.index
                )
                sig = sig & hour_ok & dow_ok

            res = run_backtest(
                df, sig,
                stop_atr=stop_atr, target_atr=target_atr,
                trail_atr=trail_atr, trail_activate_atr=trail_activate_atr,
                cost_bps=cost_bps, slippage_frac=slippage_frac,
                max_hold_bars=max_hold_bars,
            )
            runs.append(StrategyRun(symbol=sym, setup=setup, result=res))
        except Exception:
            # Individual symbol failure shouldn't sink the portfolio
            continue
    return runs


# ---------- Aggregations ----------

def equity_curve_dollar(runs: list[StrategyRun], starting_capital: float = 100_000) -> pd.Series:
    """Stitch per-strategy R-mults into a dollar-valued equity curve at fixed risk-per-trade.

    Each strategy is treated as an independent slot with equal capital share; trades sized
    at 1% of slot capital per R. Sums into a single META equity curve.
    """
    if not runs:
        return pd.Series([starting_capital], index=[pd.Timestamp.utcnow()])

    slot_cap = starting_capital / max(len(runs), 1)
    risk_per_trade = 0.01

    # Flatten all trades, sort by exit_ts
    rows = []
    for r in runs:
        for t in r.result.trades:
            rows.append((t.exit_ts, slot_cap * risk_per_trade * t.r_multiple))
    if not rows:
        return pd.Series([starting_capital], index=[pd.Timestamp.utcnow()])

    df = pd.DataFrame(rows, columns=["ts", "pnl"]).sort_values("ts")
    df["equity"] = starting_capital + df["pnl"].cumsum()
    return pd.Series(df["equity"].values, index=df["ts"].values)


def monthly_pnl_table(runs: list[StrategyRun], starting_capital: float = 100_000) -> pd.DataFrame:
    """Return DataFrame indexed by year, columns Jan..Dec + TOTAL, in dollars."""
    slot_cap = starting_capital / max(len(runs), 1)
    risk_per_trade = 0.01
    rows = []
    for r in runs:
        for t in r.result.trades:
            rows.append((t.exit_ts, slot_cap * risk_per_trade * t.r_multiple))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "pnl"])
    df["year"] = df["ts"].dt.year
    df["month"] = df["ts"].dt.month
    agg = df.groupby(["year", "month"])["pnl"].sum().unstack(fill_value=0.0)
    agg = agg.reindex(columns=range(1, 13), fill_value=0.0)
    agg["TOTAL"] = agg.sum(axis=1)
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "TOTAL"]
    agg.columns = months
    return agg


def trades_by_symbol(runs: list[StrategyRun]) -> dict[str, int]:
    return {r.symbol: r.result.n_trades for r in runs if r.result.n_trades}


# ---------- Prop-firm account simulator ----------

PROP_FIRMS = ["FTMO", "APEX", "DARWINEX"]


@dataclass
class PropAccount:
    firm: str
    symbol: str
    starting_balance: float
    challenge_cost: float
    daily_dd_limit: float
    max_dd_limit: float
    payout_threshold: float
    payout_pct: float
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    payouts: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    blown_up: bool = False
    blew_up_at: pd.Timestamp | None = None


def simulate_prop_account(
    firm: str,
    run: StrategyRun,
    starting_balance: float = 200_000,
    challenge_cost: float = 1_080,
    daily_dd_pct: float = 0.05,
    max_dd_pct: float = 0.10,
    payout_threshold_pct: float = 0.05,
    payout_pct: float = 0.80,
    leverage: float = 30.0,
) -> PropAccount:
    """One funded-account simulation. Trades scaled by leverage. Monthly payouts."""
    acc = PropAccount(
        firm=firm,
        symbol=run.symbol,
        starting_balance=starting_balance,
        challenge_cost=challenge_cost,
        daily_dd_limit=starting_balance * daily_dd_pct,
        max_dd_limit=starting_balance * max_dd_pct,
        payout_threshold=starting_balance * (1 + payout_threshold_pct),
        payout_pct=payout_pct,
    )
    if not run.result.trades:
        acc.equity_curve = pd.Series([starting_balance], index=[pd.Timestamp.utcnow()])
        return acc

    high_water = starting_balance
    equity = starting_balance
    pts: list[tuple[pd.Timestamp, float]] = []
    last_payout_month: tuple[int, int] | None = None

    for t in sorted(run.result.trades, key=lambda x: x.exit_ts):
        dollar_pnl = starting_balance * 0.01 * leverage * t.r_multiple * 0.1  # conservative scale
        equity += dollar_pnl
        pts.append((t.exit_ts, equity))

        # Blow-up check
        if equity <= starting_balance - acc.max_dd_limit:
            acc.blown_up = True
            acc.blew_up_at = t.exit_ts
            break

        # Monthly payout if above threshold
        ym = (t.exit_ts.year, t.exit_ts.month)
        if equity >= acc.payout_threshold and ym != last_payout_month:
            excess = equity - starting_balance
            payout = excess * payout_pct
            equity -= payout
            acc.payouts.append((t.exit_ts, payout))
            last_payout_month = ym
            pts.append((t.exit_ts, equity))

        high_water = max(high_water, equity)

    acc.equity_curve = pd.Series([p[1] for p in pts], index=[p[0] for p in pts])
    return acc


def simulate_prop_portfolio(runs: list[StrategyRun]) -> list[PropAccount]:
    """One account per (firm, symbol) — small realistic spread."""
    accounts: list[PropAccount] = []
    for run in runs:
        for firm in PROP_FIRMS[:2]:  # two firms per symbol => 10 accounts on 5 symbols
            accounts.append(simulate_prop_account(firm, run))
    return accounts


# ---------- API-shaped output ----------

def portfolio_summary_json(runs: list[StrategyRun], starting_capital: float = 100_000) -> dict:
    """Serialize a single META-EA strategy summary in the shape the frontend expects."""
    eq = equity_curve_dollar(runs, starting_capital)
    total_trades = sum(r.result.n_trades for r in runs)
    if total_trades == 0:
        return dict(
            net_profit=0.0, total_trades=0, win_rate=0.0,
            profit_factor=0.0, sharpe=0.0, max_dd_pct=0.0, cagr=0.0,
            equity_ts=[], equity_val=[],
            monthly=[], trades_by_symbol={},
            starting_capital=starting_capital,
        )

    wins = [t for r in runs for t in r.result.trades if t.r_multiple > 0]
    losses = [t for r in runs for t in r.result.trades if t.r_multiple <= 0]
    win_rate = len(wins) / total_trades
    gross_w = sum(t.r_multiple for t in wins)
    gross_l = -sum(t.r_multiple for t in losses) or 1e-9
    profit_factor = gross_w / gross_l

    peak = eq.cummax()
    max_dd_pct = float(((eq / peak) - 1).min())
    span_days = max((eq.index[-1] - eq.index[0]).total_seconds() / 86400.0, 1.0)
    cagr = float((eq.iloc[-1] / starting_capital) ** (365.25 / span_days) - 1)

    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if len(rets) > 2 and rets.std() else 0.0

    monthly = monthly_pnl_table(runs, starting_capital)
    monthly_records = []
    for year, row in monthly.iterrows():
        rec = {"year": int(year)}
        rec.update({col: float(row[col]) for col in monthly.columns})
        monthly_records.append(rec)

    return dict(
        net_profit=float(eq.iloc[-1] - starting_capital),
        final_equity=float(eq.iloc[-1]),
        total_trades=total_trades,
        win_rate=float(win_rate),
        profit_factor=float(profit_factor),
        sharpe=sharpe,
        max_dd_pct=max_dd_pct,
        cagr=float(cagr),
        starting_capital=starting_capital,
        equity_ts=[t.isoformat() for t in eq.index],
        equity_val=[float(v) for v in eq.values],
        monthly=monthly_records,
        trades_by_symbol=trades_by_symbol(runs),
    )


def prop_portfolio_summary_json(accounts: list[PropAccount]) -> dict:
    """Serialize the prop-firm-portfolio view (top KPI tiles + cashflow)."""
    total_payouts = sum(p for a in accounts for _, p in a.payouts)
    total_costs = sum(a.challenge_cost for a in accounts)
    blow_ups = sum(1 for a in accounts if a.blown_up)
    payout_count = sum(len(a.payouts) for a in accounts)

    # Monthly cashflow (payouts +, costs centred on first month per account)
    rows = []
    for a in accounts:
        for ts, p in a.payouts:
            rows.append((pd.Timestamp(ts).to_period("M"), "payout", p))
        if a.equity_curve.size:
            rows.append((pd.Timestamp(a.equity_curve.index[0]).to_period("M"), "cost", -a.challenge_cost))
    cashflow_df = pd.DataFrame(rows, columns=["month", "kind", "amount"])
    cashflow: list[dict] = []
    if not cashflow_df.empty:
        agg = cashflow_df.groupby(["month", "kind"])["amount"].sum().unstack(fill_value=0.0)
        for month, row in agg.iterrows():
            cashflow.append({
                "month": str(month),
                "payout": float(row.get("payout", 0.0)),
                "cost": float(row.get("cost", 0.0)),
            })

    # Per-account list
    accs_json = []
    for a in accounts:
        final_eq = float(a.equity_curve.iloc[-1]) if a.equity_curve.size else a.starting_balance
        accs_json.append(dict(
            firm=a.firm,
            symbol=a.symbol,
            starting_balance=a.starting_balance,
            final_equity=final_eq,
            payouts=float(sum(p for _, p in a.payouts)),
            payout_count=len(a.payouts),
            blown_up=a.blown_up,
            challenge_cost=a.challenge_cost,
        ))

    return dict(
        accounts=len(accounts),
        blow_ups=blow_ups,
        attempts=len(accounts),
        total_cost=total_costs,
        payouts=payout_count,
        total_paid_out=float(total_payouts),
        net_profit=float(total_payouts - total_costs),
        roi=float((total_payouts - total_costs) / total_costs) if total_costs > 0 else 0.0,
        final_phase="Funded" if blow_ups < len(accounts) / 2 else "Mixed",
        cashflow=cashflow,
        account_list=accs_json,
    )
