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
    Stage 1. Pull Massive.com flatfiles -> upload raw ``day_aggs_v1`` /
    ``minute_aggs_v1`` csv.gz to MinIO -> pivot into per-ticker Parquet.
    Idempotent and self-healing (checkpoint + lookback gap-scan).
build_bars
    Stage 2. Idempotent, resumable, parallel-by-date ingest of the raw csv.gz
    into ``bars/tf=<tf>/date=<date>/part.parquet``.
dagster_defs
    Dagster definitions (job + daily schedule) that run stage 1 then stage 2.
"""

from __future__ import annotations

__all__ = ["minio_io", "ingest_massive", "build_bars"]
