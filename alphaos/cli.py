"""CLI entry points for V2-FRONTIER.

  python -m alphaos.cli serve                 # launches the web dashboard
  python -m alphaos.cli db upgrade            # run Alembic migrations to head
  python -m alphaos.cli db current            # show current migration revision
  python -m alphaos.cli db seed               # seed the V2-FRONTIER catalog
  python -m alphaos.cli fx refresh            # refresh USD/EUR -> SEK rates
  python -m alphaos.cli prices refresh        # refresh latest prices from MinIO
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


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
    from .db import have_database, session_scope
    from .db import allocation as zalloc
    from .db import config as zconfig

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
            n = zalloc.seed_default_sleeves(s)
            zconfig.get_config(s)
        print(f"Seeded {n} sleeve(s) into the V2-FRONTIER catalog.")
        return 0

    if args.action == "current":
        from alembic.config import Config
        from alembic import command
        cfg = Config(str(_alembic_ini_path()))
        command.current(cfg)
        return 0

    return 1


def cmd_fx(args: argparse.Namespace) -> int:
    """Foreign-exchange rate commands."""
    from .db import have_database, session_scope
    from .db import fx as dbfx

    if not have_database():
        print("No database is configured (set ALPHAOS_DATABASE_URL / DATABASE_URL "
              "or PG* environment variables).", file=sys.stderr)
        return 1

    if args.action == "refresh":
        with session_scope() as s:
            result = dbfx.refresh_fx(s)
        print(result)
        return 0 if result.get("ok") else 1

    return 1


def cmd_prices(args: argparse.Namespace) -> int:
    """Market-price refresh commands."""
    from .db import have_database, session_scope
    from .db import pricing as dbpricing

    if not have_database():
        print("No database is configured (set ALPHAOS_DATABASE_URL / DATABASE_URL "
              "or PG* environment variables).", file=sys.stderr)
        return 1

    if args.action == "refresh":
        with session_scope() as s:
            result = dbpricing.refresh_prices(s)
        print(result)
        return 0 if result.get("ok") else 1

    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alphaos")
    sub = p.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("serve", help="Run the FastAPI web dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8503)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    db = sub.add_parser("db", help="Database / migration ops")
    db.add_argument("action", choices=["upgrade", "seed", "current"])
    db.set_defaults(func=cmd_db)

    fx = sub.add_parser("fx", help="Foreign-exchange rate ops")
    fx.add_argument("action", choices=["refresh"])
    fx.set_defaults(func=cmd_fx)

    prices = sub.add_parser("prices", help="Market-price refresh ops")
    prices.add_argument("action", choices=["refresh"])
    prices.set_defaults(func=cmd_prices)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
