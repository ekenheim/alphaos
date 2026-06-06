"""CLI entry points.

  python -m alphaos.cli scan SPY --setup orb_break
  python -m alphaos.cli backtest SPY --setup orb_break --interval 5m
  python -m alphaos.cli serve                 # launches the web dashboard
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import data as zdata
from . import levels as zlevels
from . import setups as zsetups
from .backtest import run_backtest
from .placebo import run_placebo


def cmd_scan(args: argparse.Namespace) -> int:
    df = zdata.fetch_ohlcv(args.symbol, interval=args.interval)
    df = zlevels.attach_all_levels(df, or_minutes=args.or_minutes)
    sig = zsetups.detect(args.setup, df)
    hits = df[sig].tail(args.last)
    print(f"{args.symbol} {args.interval} {args.setup}: {sig.sum()} signals in window, showing last {len(hits)}")
    if len(hits):
        print(hits[["open", "high", "low", "close", "pdh", "orh", "vwap"]].to_string())
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    df = zdata.fetch_ohlcv(args.symbol, interval=args.interval)
    df = zlevels.attach_all_levels(df, or_minutes=args.or_minutes)
    sig = zsetups.detect(args.setup, df)
    res = run_backtest(
        df, sig,
        stop_atr=args.stop_atr, target_atr=args.target_atr,
        max_hold_bars=args.max_hold, cost_bps=args.cost_bps,
        slippage_frac=args.slippage,
    )
    print(f"=== {args.symbol} {args.interval} {args.setup} ===")
    print(f"Trades:    {res.n_trades}")
    print(f"Win rate:  {res.win_rate*100:.1f}%")
    print(f"Avg R:     {res.avg_r:+.2f}")
    print(f"Sharpe:    {res.sharpe:+.2f}")
    print(f"Max DD:    {res.max_dd*100:+.1f}%")
    print(f"CAGR-eq:   {res.cagr*100:+.2f}%")

    if args.placebo:
        print(f"\nRunning PLACEBO ({args.placebo} sticky-Markov sims)...")
        pl = run_placebo(df, sig, n_runs=args.placebo,
                         stop_atr=args.stop_atr, target_atr=args.target_atr,
                         max_hold_bars=args.max_hold, cost_bps=args.cost_bps,
                         slippage_frac=args.slippage)
        verdict = "PASS" if pl.passed else "FAIL"
        print(f"PLACEBO {verdict}: real Sh={pl.real_sharpe:+.2f}  P95={pl.p95:+.2f}  rank={pl.rank_pct*100:.0f}%")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI web dashboard."""
    import uvicorn
    uvicorn.run("alphaos.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """Paper-trade ledger commands."""
    from . import paper
    if args.action == "scan":
        rows = paper.scan_today()
        if rows.empty:
            print("No fresh signals.")
        else:
            print(f"Logged {len(rows)} new signal(s):")
            for _, r in rows.iterrows():
                print(f"  {r['signal_ts']}  {r['symbol']:<8} {r['setup']:<14} "
                      f"long @ {r['entry_px']:.2f}  stop {r['stop_px']:.2f}  target {r['target_px']:.2f}")
        return 0
    if args.action == "mark":
        closed = paper.mark_to_market()
        print(f"Closed {closed} position(s).")
        return 0
    if args.action == "status":
        s = paper.summary()
        print(f"Closed: {s['closed']}  Open: {s['open']}")
        print(f"Win rate:     {s['win_rate']*100:.1f}%")
        print(f"Profit factor: {s['pf']:.2f}")
        print(f"Avg R:        {s['avg_r']:+.2f}")
        print(f"Total R:      {s['total_r']:+.2f}")
        print(f"Cum equity:   {s['cum_equity']:.3f}  ({(s['cum_equity']-1)*100:+.2f}%)")
        if s["by_symbol"]:
            print("\nBy symbol:")
            for sym, st in s["by_symbol"].items():
                print(f"  {sym:<8} n={st['count']:.0f}  totR={st['sum']:+.2f}  avgR={st['mean']:+.2f}")
        if s["by_setup"]:
            print("\nBy setup:")
            for setup, st in s["by_setup"].items():
                print(f"  {setup:<16} n={st['count']:.0f}  totR={st['sum']:+.2f}  avgR={st['mean']:+.2f}")
        return 0
    if args.action == "ledger":
        from . import paper
        ledger = paper.load_ledger()
        if ledger.empty:
            print("Empty ledger.")
        else:
            with pd.option_context("display.max_columns", None, "display.width", 200):
                print(ledger.tail(args.last).to_string())
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alphaos")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("symbol", help="e.g. SPY, QQQ, ES, BTC, EURUSD")
    common.add_argument("--interval", default="5m", choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    common.add_argument("--setup", default="orb_break", choices=list(zsetups.SETUPS))
    common.add_argument("--or-minutes", type=int, default=15)

    s = sub.add_parser("scan", parents=[common])
    s.add_argument("--last", type=int, default=10)
    s.set_defaults(func=cmd_scan)

    b = sub.add_parser("backtest", parents=[common])
    b.add_argument("--stop-atr", type=float, default=1.0)
    b.add_argument("--target-atr", type=float, default=2.0)
    b.add_argument("--max-hold", type=int, default=78)
    b.add_argument("--cost-bps", type=float, default=1.0)
    b.add_argument("--slippage", type=float, default=0.05)
    b.add_argument("--placebo", type=int, default=0, help="N placebo runs (0=skip)")
    b.set_defaults(func=cmd_backtest)

    sv = sub.add_parser("serve", help="Run the FastAPI web dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8503)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    pp = sub.add_parser("paper", help="Paper-trade ledger ops")
    pp.add_argument("action", choices=["scan", "mark", "status", "ledger"])
    pp.add_argument("--last", type=int, default=20)
    pp.set_defaults(func=cmd_paper)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
