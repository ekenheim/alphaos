"""CLI entry points.

  python -m alphaos.cli scan SPY --setup orb_break
  python -m alphaos.cli backtest SPY --setup orb_break --interval 5m
  python -m alphaos.cli serve                 # launches the web dashboard
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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

    placebo_pass = None
    if args.placebo:
        print(f"\nRunning PLACEBO ({args.placebo} sticky-Markov sims)...")
        pl = run_placebo(df, sig, n_runs=args.placebo,
                         stop_atr=args.stop_atr, target_atr=args.target_atr,
                         max_hold_bars=args.max_hold, cost_bps=args.cost_bps,
                         slippage_frac=args.slippage)
        placebo_pass = pl.passed
        verdict = "PASS" if pl.passed else "FAIL"
        print(f"PLACEBO {verdict}: real Sh={pl.real_sharpe:+.2f}  P95={pl.p95:+.2f}  rank={pl.rank_pct*100:.0f}%")

    if args.save:
        from .db import have_database, session_scope, archive
        if not have_database():
            print("--save requested but no database is configured.", file=sys.stderr)
            return 1
        with session_scope() as s:
            bt = archive.save_backtest(
                s,
                strategy_slug=args.setup,
                symbol=args.symbol,
                interval=args.interval,
                n_trades=res.n_trades,
                win_rate=res.win_rate,
                avg_r=res.avg_r,
                sharpe=res.sharpe,
                max_dd=res.max_dd,
                cagr=res.cagr,
                params={
                    "stop_atr": args.stop_atr,
                    "target_atr": args.target_atr,
                    "max_hold_bars": args.max_hold,
                    "cost_bps": args.cost_bps,
                    "slippage_frac": args.slippage,
                },
                placebo_pass=placebo_pass,
            )
            print(f"Saved backtest id={bt.id}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI web dashboard."""
    import uvicorn
    uvicorn.run("alphaos.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


def _alembic_ini_path() -> Path:
    """Resolve the alembic.ini path from the first existing candidate."""
    candidates = [
        os.environ.get("ALPHAOS_ALEMBIC_INI"),
        Path.cwd() / "alembic.ini",
        Path(__file__).resolve().parent.parent / "alembic.ini",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return Path(cand)
    # Fall back to the package-relative path even if missing, for a clear error.
    return Path(__file__).resolve().parent.parent / "alembic.ini"


def cmd_db(args: argparse.Namespace) -> int:
    """Database / migration commands."""
    from .db import have_database, session_scope, archive

    if not have_database():
        print("No database is configured (set ALPHAOS_DATABASE_URL / DATABASE_URL "
              "or PG* environment variables).", file=sys.stderr)
        return 1

    if args.action == "upgrade":
        from alembic.config import Config
        from alembic import command
        cfg = Config(str(_alembic_ini_path()))
        command.upgrade(cfg, "head")
        print("Migrations upgraded to head.")
        return 0

    if args.action == "seed":
        with session_scope() as s:
            n = archive.seed_strategies_from_setups(s)
        print(f"Seeded {n} strategy(ies) from setups.")
        return 0

    if args.action == "current":
        from alembic.config import Config
        from alembic import command
        cfg = Config(str(_alembic_ini_path()))
        command.current(cfg)
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
    b.add_argument("--save", action="store_true",
                   help="Persist results to the database (requires a configured DB)")
    b.set_defaults(func=cmd_backtest)

    sv = sub.add_parser("serve", help="Run the FastAPI web dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8503)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    db = sub.add_parser("db", help="Database / migration ops")
    db.add_argument("action", choices=["upgrade", "seed", "current"])
    db.set_defaults(func=cmd_db)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
