"""Market-data pipeline for the canonical ``bars/`` corpus in MinIO.

This package is the daily US-equities pipeline that produces the
survivorship-free, split-UNADJUSTED, date-partitioned Parquet corpus that
AlphaOS reads for US-stock pricing (see ``alphaos/db/pricing.py``).

Modules
-------
minio_io
    Shared MinIO/S3 + DuckDB connection plumbing, env resolution, and the
    constants (timeframes, schema, layout) the other modules import.
ingest_massive
    Stage 1. Mirror Massive.com flatfiles to MinIO raw (``day_aggs_v1`` /
    ``minute_aggs_v1`` csv.gz), exactly as the vendor provides them. The Dagster
    job runs this with --no-pivot, so the legacy per-ticker pivot/resample is not
    used; build_bars owns the corpus and all resampling. Idempotent, self-healing
    (lookback gap-scan).
build_bars
    Stage 2. Idempotent, resumable, parallel-by-date ingest of the raw csv.gz
    into ``bars/tf=<tf>/date=<date>/part.parquet``.
dagster_defs
    Dagster definitions (job + daily schedule) that run stage 1 then stage 2.
"""

from __future__ import annotations

__all__ = ["minio_io", "ingest_massive", "build_bars"]
