"""Dagster definitions for the daily market-data pipeline.

One job, ``market_data_daily``, with two ordered ops:

    ingest_massive_op  -> build_bars_op

Stage 1 (``ingest_massive``) pulls Massive.com flatfiles into MinIO; stage 2
(``build_bars``) builds the ``bars/tf=<tf>/date=<date>/part.parquet`` corpus that
AlphaOS reads. Both stages are idempotent and self-healing, so re-runs and
weekend/holiday runs (when Massive publishes nothing) are safe no-ops.

This module is the Dagster code location. Point a gRPC user-deployment at it:

    dagster api grpc -h 0.0.0.0 -p 4000 -m marketdata.dagster_defs

It needs the same env the scripts need: ``MINIO_*`` (write-capable) and
``MASSIVE_S3_*``. See DEPLOYMENT.md.

Note: this module intentionally does NOT use ``from __future__ import
annotations`` -- Dagster introspects the pydantic ``Config`` annotations at
import time and cannot resolve them if they are stringized.
"""

from datetime import date, timedelta

from dagster import (
    Config,
    Definitions,
    Failure,
    In,
    Nothing,
    OpExecutionContext,
    ScheduleDefinition,
    in_process_executor,
    job,
    op,
)

from . import build_bars as build_bars_mod
from . import ingest_massive as ingest_mod

# --------------------------------------------------------------------------- #
# Run-pod resources. The K8sRunLauncher reads this tag and applies it to the
# per-run pod. The minute build/pivot is the heavy stage -- size for it. Tune
# to observed usage; start generous on memory (DuckDB + many worker threads).
# --------------------------------------------------------------------------- #
K8S_RUN_CONFIG = {
    "container_config": {
        "resources": {
            "requests": {"cpu": "2", "memory": "4Gi"},
            "limits": {"cpu": "8", "memory": "16Gi"},
        },
        # Inject MinIO (write-capable) + Massive creds into THIS job's run pods
        # only, so other code locations' run pods are untouched (avoids clobbering
        # any shared MINIO_* env on the global run launcher). The named Secrets
        # must exist in the run launcher's jobNamespace (e.g. datasci):
        #   alphaos-minio-rw -> MINIO_ENDPOINT_URL / MINIO_ACCESS_KEY_ID /
        #                       MINIO_SECRET_ACCESS_KEY (write-capable)
        #   alphaos-massive  -> MASSIVE_S3_ACCESS_KEY_ID / MASSIVE_S3_SECRET_ACCESS_KEY
        "env_from": [
            {"secret_ref": {"name": "alphaos-minio-rw"}},
            {"secret_ref": {"name": "alphaos-massive"}},
        ],
        "env": [
            {"name": "MINIO_BUCKET", "value": "stocks-us"},
        ],
    }
}


# --------------------------------------------------------------------------- #
# Op config. Defaults give a cheap daily incremental; override from the Dagster
# launchpad for backfills (e.g. lookback_days: 3650, rebuild: true).
# --------------------------------------------------------------------------- #
class IngestConfig(Config):
    # Calendar-day lookback gap-scan when no explicit window is given.
    lookback_days: int = 7
    # None -> both day_aggs_v1 and minute_aggs_v1.
    datasets: list[str] | None = None
    # Escape hatch: extra raw CLI flags appended verbatim (e.g. ["--force"]).
    extra_args: list[str] = []


class BuildBarsConfig(Config):
    # Build partitions for the trailing window; already-built dates are skipped.
    # Set null/None to scan the entire raw archive (first-run full backfill).
    lookback_days: int | None = 14
    # None -> all four timeframes (1day, 1min, 5min, 15min).
    timeframes: list[str] | None = None
    # Rebuild partitions even if already present.
    rebuild: bool = False
    extra_args: list[str] = []


@op
def ingest_massive_op(context: OpExecutionContext, config: IngestConfig) -> None:
    """Stage 1: mirror Massive.com flatfiles to MinIO raw, nothing else.

    Uploads the raw day_aggs_v1/ + minute_aggs_v1/ csv.gz exactly as the vendor
    provides them. The legacy per-ticker pivot/resample (daily/{TICKER}.parquet,
    1min/, 5min/, 15min/) is intentionally DISABLED (--no-pivot): the corpus and
    all resampling are produced downstream by build_bars, date-partitioned and
    idempotent. So this stage never touches per-ticker files.
    """
    # --no-pivot is hardcoded: raw mirror only, no per-ticker output.
    argv: list[str] = ["--lookback-days", str(config.lookback_days), "--no-pivot"]
    if config.datasets:
        argv += ["--datasets", *config.datasets]
    argv += config.extra_args

    context.log.info("Running ingest_massive (raw mirror only) %s", argv)
    rc = ingest_mod.main(argv)
    if rc != 0:
        raise Failure(f"ingest_massive exited with code {rc}")
    context.log.info("ingest_massive complete (raw flatfiles mirrored).")


@op(ins={"start": In(Nothing)})
def build_bars_op(context: OpExecutionContext, config: BuildBarsConfig) -> None:
    """Stage 2: raw flatfiles -> bars/tf=*/date=*/part.parquet corpus.

    Depends on ``ingest_massive_op`` via the ``Nothing`` input so the corpus is
    only built after the day's raw flatfiles have landed.
    """
    argv: list[str] = []
    if config.timeframes:
        argv += ["--timeframes", *config.timeframes]
    if config.lookback_days is not None:
        start = (date.today() - timedelta(days=config.lookback_days)).isoformat()
        argv += ["--start", start]
    if config.rebuild:
        argv += ["--rebuild"]
    argv += config.extra_args

    context.log.info("Running build_bars %s", argv)
    rc = build_bars_mod.main(argv)
    if rc != 0:
        raise Failure(f"build_bars exited with code {rc}")
    context.log.info("build_bars complete.")


@job(
    tags={"dagster-k8s/config": K8S_RUN_CONFIG},
    executor_def=in_process_executor,
)
def market_data_daily():
    build_bars_op(start=ingest_massive_op())


# Daily at 07:00 UTC: the prior US session's Massive flatfiles (published after
# the close) are available, and AlphaOS's existing alphaos-daily-snapshot CronJob
# (30 22 * * 1-5 UTC) then values holdings against the fresh corpus. Tune to your
# Massive publish lag. Schedules load STOPPED; start it from the Dagster UI.
market_data_daily_schedule = ScheduleDefinition(
    name="market_data_daily_schedule",
    job=market_data_daily,
    cron_schedule="0 7 * * *",
    execution_timezone="UTC",
)


defs = Definitions(
    jobs=[market_data_daily],
    schedules=[market_data_daily_schedule],
)
