"""Daily snapshot job.

Refresh FX + MinIO daily closes, then persist exactly one derived NAV snapshot
for today (idempotent). This is the entry point for the cluster CronJob; the
CronJob manifest lives in the SEPARATE Flux/GitOps repo (see DEPLOYMENT.md) — this
module ships as the `alphaos-daily-snapshot` console script and needs no scheduler
to run. Importable and side-effect-free at import time: all work happens in main().
"""

from __future__ import annotations

import datetime as dt

from .db import session_scope
from .db import fx as dbfx
from .db import nav as dbnav
from .db import pricing as dbpricing


def main() -> int:
    """Run the daily refresh + snapshot once. Returns a process exit code (0 = ok).

    Steps (all inside one transactional session that commits on success):
      1. refresh_fx       — never raises; keeps cached rates if egress fails.
      2. refresh_prices   — guarded by MinIO have_credentials(); no-op without creds.
      3. upsert_snapshot  — replace today's derived NAV snapshot (idempotent).
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    with session_scope() as session:  # commits on success, rolls back on error
        fx_res = dbfx.refresh_fx(session)
        if dbpricing.have_credentials():
            px_res = dbpricing.refresh_prices(session)
        else:
            px_res = {"ok": False, "error": "MinIO credentials not configured", "updated": 0}
        snap = dbnav.upsert_snapshot(session, as_of=today)
        print(
            f"[alphaos-daily-snapshot] {today}: "
            f"fx ok={fx_res.get('ok')} src={fx_res.get('source')}; "
            f"prices ok={px_res.get('ok')} updated={px_res.get('updated')}; "
            f"snapshot equity={snap.equity} nav_index={snap.nav_index} "
            f"drawdown={snap.drawdown}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
