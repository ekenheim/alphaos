#!/usr/bin/env python3
"""
Build the canonical ``bars/`` corpus in MinIO from the raw csv.gz flatfiles.

Reads the immutable raw archive already in MinIO

    day_aggs_v1/YYYY/MM/YYYY-MM-DD.csv.gz      (official daily, all tickers)
    minute_aggs_v1/YYYY/MM/YYYY-MM-DD.csv.gz   (1-minute, all tickers, pre/post)

and writes Hive-partitioned, RAW + split-UNADJUSTED Parquet

    bars/tf=1day/date=YYYY-MM-DD/part.parquet     <- official daily aggregates
    bars/tf=1min/date=YYYY-MM-DD/part.parquet     <- 1:1 passthrough of minute
    bars/tf=5min/date=YYYY-MM-DD/part.parquet     <- resampled from raw 1min
    bars/tf=15min/date=YYYY-MM-DD/part.parquet    <- resampled from raw 1min
    bars/tf=<tf>/_meta.json                       <- schema/tz/range/rows/built-at

Design guarantees (see marketdata/README.md for the full contract):
  * RAW + IMMUTABLE: prices are stored exactly as the vendor provides them,
    UNADJUSTED for splits/dividends. Adjustment is applied at QUERY TIME by
    marketdata.reader from the reference tables.
  * SURVIVORSHIP-FREE: every ticker that traded on a date is in that date's
    partition, including later-delisted names. No liquidity/existence filter.
  * UNIFORM SCHEMA across all timeframes (ticker, ts, ohlc, volume, transactions).
  * IDEMPOTENT + RESUMABLE: a partition already present is skipped unless
    --rebuild; re-running a date deterministically overwrites just that date.
  * VALIDATED: OHLC sanity, no duplicate (ticker, ts), and row-count
    reconciliation gate every write. A partition that fails is NOT written.

Examples
--------
  # Full backfill, all four timeframes, over everything in the raw archive.
  python -m marketdata.build_bars

  # Just the daily timeframe (fast -- daily files are tiny).
  python -m marketdata.build_bars --timeframes 1day

  # Rebuild a single date end to end.
  python -m marketdata.build_bars --start 2024-06-10 --end 2024-06-10 --rebuild

  # Minute family only, more parallelism, capped DuckDB memory per worker.
  python -m marketdata.build_bars --timeframes 1min 5min 15min --workers 12 \
      --memory-limit 4GB
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

from . import minio_io
from .minio_io import (
    BAR_COLUMN_TYPES,
    BAR_COLUMNS,
    DEFAULT_COMPRESSION,
    DEFAULT_ROW_GROUP_SIZE,
    RESAMPLE_MINUTES,
    SOURCE_DATASET,
    TIMEFRAMES,
    TS_CONVENTION,
    WINDOW_START_UNIT,
    MinioConfig,
)

DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\.csv\.gz$")

# Explicit column spec for read_csv. volume/transactions are DOUBLE in the raw
# source (occasionally large floats) and cast to BIGINT downstream.
CSV_COLUMNS_SQL = (
    "{'ticker':'VARCHAR','volume':'DOUBLE','open':'DOUBLE','close':'DOUBLE',"
    "'high':'DOUBLE','low':'DOUBLE','window_start':'BIGINT','transactions':'DOUBLE'}"
)

# Validity filters: drop rows the vendor can't give a usable bar for.
DAILY_VALID = (
    "ticker IS NOT NULL AND open IS NOT NULL AND high IS NOT NULL "
    "AND low IS NOT NULL AND close IS NOT NULL"
)
MINUTE_VALID = "window_start IS NOT NULL AND " + DAILY_VALID

# Colliding/recycled tickers (e.g. OP, BCPC, TPC, META, FB...) put two distinct
# securities under one symbol on the same day. The flatfile carries no FIGI to
# tell them apart, and the corpus contract requires a unique (ticker, ts) key,
# so we deterministically keep the dominant print: highest volume, then most
# transactions, then highest close. Every dropped row is counted as a collision
# and surfaced in the build report -- never silently merged.
DEDUP_ORDER = "volume DESC, transactions DESC, close DESC"

_AUDIT_DIR = Path(__file__).resolve().parent.parent / "_audit"

# One DuckDB connection per worker thread, lazily created and reused.
_thread_local = threading.local()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def raw_key_date(key: str) -> date | None:
    match = DATE_RE.search(key)
    return parse_date(match.group("date")) if match else None


def worker_connection(config: MinioConfig, memory_limit: str | None) -> "duckdb.DuckDBPyConnection":  # noqa: F821
    """Return this thread's DuckDB connection, creating it on first use."""
    con = getattr(_thread_local, "con", None)
    if con is None:
        con = minio_io.connect_duckdb(config, threads=1)
        if memory_limit:
            con.execute(f"PRAGMA memory_limit='{memory_limit}'")
        _thread_local.con = con
    return con


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def discover_source_dates(
    s3,
    bucket: str,
    dataset: str,
    start: date | None,
    end: date | None,
) -> dict[date, int]:
    """Map ``{date: size_bytes}`` for every raw csv.gz of ``dataset`` in range."""
    found: dict[date, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{dataset}/"):
        for obj in page.get("Contents", []):
            d = raw_key_date(obj["Key"])
            if d is None:
                continue
            if start is not None and d < start:
                continue
            if end is not None and d > end:
                continue
            found[d] = int(obj["Size"])
    return found


def discover_existing_partitions(s3, bucket: str, timeframe: str) -> set[date]:
    """Set of dates that already have a built partition for ``timeframe``."""
    existing: set[date] = set()
    paginator = s3.get_paginator("list_objects_v2")
    prefix = f"{minio_io.BARS_PREFIX}/tf={timeframe}/date="
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            m = re.search(r"date=(\d{4}-\d{2}-\d{2})/part\.parquet$", obj["Key"])
            if m:
                existing.add(parse_date(m.group(1)))
    return existing


# --------------------------------------------------------------------------- #
# SQL builders
# --------------------------------------------------------------------------- #

def _load_raw_sql(config: MinioConfig, dataset: str, day: date) -> str:
    """SELECT that reads one raw csv.gz into the normalised raw columns."""
    key = f"{dataset}/{day.year:04d}/{day.month:02d}/{day.isoformat()}.csv.gz"
    uri = minio_io.s3_uri(config, key)
    return f"""
        SELECT
            upper(ticker) AS ticker,
            CAST(open AS DOUBLE) AS open,
            CAST(high AS DOUBLE) AS high,
            CAST(low AS DOUBLE) AS low,
            CAST(close AS DOUBLE) AS close,
            CAST(COALESCE(volume, 0) AS BIGINT) AS volume,
            CAST(window_start AS BIGINT) AS window_start,
            CAST(COALESCE(transactions, 0) AS BIGINT) AS transactions
        FROM read_csv('{uri}', header=true, compression='gzip', columns={CSV_COLUMNS_SQL})
    """


def _daily_source_sql(day: date) -> str:
    """1day output: one bar per ticker, ts = midnight UTC of the ET trading date.

    Deduped to one row per ticker (see DEDUP_ORDER) so colliding symbols can't
    produce a duplicate key.
    """
    return f"""
        SELECT ticker, ts, open, high, low, close, volume, transactions
        FROM (
            SELECT
                ticker,
                TIMESTAMP '{day.isoformat()} 00:00:00' AS ts,
                open, high, low, close, volume, transactions,
                row_number() OVER (PARTITION BY ticker ORDER BY {DEDUP_ORDER}) AS _rn
            FROM raw
            WHERE {DAILY_VALID}
        )
        WHERE _rn = 1
    """


def _minute_source_sql() -> str:
    """1min output: 1:1 passthrough, ts = true UTC wall-clock (ns -> us).

    Deduped to one row per (ticker, ts) so colliding symbols can't produce a
    duplicate key -- and so the 5/15min resample (which reads this table) does
    not double-count a colliding ticker's volume.
    """
    return f"""
        SELECT ticker, ts, open, high, low, close, volume, transactions
        FROM (
            SELECT
                ticker,
                make_timestamp(window_start // 1000) AS ts,
                open, high, low, close, volume, transactions,
                row_number() OVER (
                    PARTITION BY ticker, make_timestamp(window_start // 1000)
                    ORDER BY {DEDUP_ORDER}) AS _rn
            FROM raw
            WHERE {MINUTE_VALID}
        )
        WHERE _rn = 1
    """


def _resample_sql(minutes: int) -> str:
    """Resample the in-memory ``src_min`` table to N-minute OHLCV buckets.

    Buckets floor the UTC minute to the N-minute boundary (time_bucket anchors
    on clock marks, so 13:30 UTC = 09:30 ET RTH open lands on a boundary).
    Empty buckets are simply absent -- never forward-filled.
    """
    bucket = f"time_bucket(INTERVAL '{minutes} minutes', ts)"
    return f"""
        SELECT
            ticker,
            {bucket} AS ts,
            first(open ORDER BY ts)  AS open,
            max(high)                AS high,
            min(low)                 AS low,
            last(close ORDER BY ts)  AS close,
            sum(volume)              AS volume,
            sum(transactions)        AS transactions
        FROM src_min
        GROUP BY ticker, {bucket}
    """


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_table(con, table: str) -> list[str]:
    """Run the data-quality gate on a built table; return violation messages.

    An empty list means the partition passed and is safe to write.
    """
    violations: list[str] = []

    row = con.execute(
        f"""
        SELECT
            count(*) FILTER (WHERE NOT (high >= greatest(open, close, low))) AS bad_high,
            count(*) FILTER (WHERE NOT (low  <= least(open, close, high)))   AS bad_low,
            count(*) FILTER (WHERE NOT (open > 0 AND high > 0
                                        AND low > 0 AND close > 0))          AS nonpos_price,
            count(*) FILTER (WHERE volume < 0)                               AS neg_volume,
            count(*)                                                         AS n
        FROM {table}
        """
    ).fetchone()
    bad_high, bad_low, nonpos_price, neg_volume, n = row

    if n == 0:
        return ["empty: 0 rows"]
    if bad_high:
        violations.append(f"ohlc: {bad_high} rows with high < max(open,close,low)")
    if bad_low:
        violations.append(f"ohlc: {bad_low} rows with low > min(open,close,high)")
    if nonpos_price:
        violations.append(f"price: {nonpos_price} rows with a non-positive O/H/L/C")
    if neg_volume:
        violations.append(f"volume: {neg_volume} rows with negative volume")

    dup = con.execute(
        f"""
        SELECT count(*) FROM (
            SELECT ticker, ts FROM {table} GROUP BY ticker, ts HAVING count(*) > 1
        )
        """
    ).fetchone()[0]
    if dup:
        violations.append(f"duplicate: {dup} repeated (ticker, ts) keys")

    return violations


# --------------------------------------------------------------------------- #
# Per-date build
# --------------------------------------------------------------------------- #

def build_one_partition(
    con,
    config: MinioConfig,
    timeframe: str,
    day: date,
    out_table: str,
    expected_rows: int,
    compression: str,
    row_group_size: int,
) -> dict:
    """Validate ``out_table`` and, if clean, COPY it to the date partition.

    Returns a per-partition result dict.
    """
    violations = validate_table(con, out_table)
    written = con.execute(f"SELECT count(*) FROM {out_table}").fetchone()[0]

    if violations:
        return {
            "timeframe": timeframe,
            "date": day.isoformat(),
            "status": "validation_failed",
            "rows": int(written),
            "violations": violations,
        }

    # Row-count reconciliation against the expected source-derived count.
    if expected_rows is not None and written != expected_rows:
        return {
            "timeframe": timeframe,
            "date": day.isoformat(),
            "status": "reconcile_failed",
            "rows": int(written),
            "violations": [f"row-count: wrote {written}, expected {expected_rows}"],
        }

    uri = minio_io.s3_uri(config, minio_io.partition_key(timeframe, day.isoformat()))
    cols = ", ".join(BAR_COLUMNS)
    con.execute(
        f"""
        COPY (SELECT {cols} FROM {out_table} ORDER BY ticker, ts)
        TO '{uri}'
        (FORMAT PARQUET, COMPRESSION {compression.upper()}, ROW_GROUP_SIZE {row_group_size})
        """
    )
    return {
        "timeframe": timeframe,
        "date": day.isoformat(),
        "status": "written",
        "rows": int(written),
        "violations": [],
    }


def build_date(
    config: MinioConfig,
    day: date,
    timeframes: list[str],
    skip: dict[str, set[date]],
    rebuild: bool,
    compression: str,
    row_group_size: int,
    memory_limit: str | None,
) -> list[dict]:
    """Build every requested, not-yet-present timeframe for one trading date.

    The minute family (1min/5min/15min) shares a single read of the raw minute
    file; 1day reads the daily file. Each timeframe is validated and written
    independently so one failure never corrupts another.
    """
    results: list[dict] = []
    con = worker_connection(config, memory_limit)

    want = [
        tf for tf in timeframes
        if rebuild or day not in skip.get(tf, set())
    ]
    if not want:
        return results

    need_daily = "1day" in want
    need_minute = any(tf in ("1min", "5min", "15min") for tf in want)

    # ---- daily ----------------------------------------------------------- #
    if need_daily:
        try:
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE raw AS {_load_raw_sql(config, SOURCE_DATASET['1day'], day)}"
            )
            con.execute(f"CREATE OR REPLACE TEMP TABLE out_1day AS {_daily_source_sql(day)}")
            valid, unique = con.execute(
                f"SELECT count(*), count(DISTINCT ticker) FROM raw WHERE {DAILY_VALID}"
            ).fetchone()
            res = build_one_partition(
                con, config, "1day", day, "out_1day", unique,
                compression, row_group_size,
            )
            res["collisions"] = int(valid - unique)
            results.append(res)
        except Exception as exc:  # noqa: BLE001 - record and continue other dates
            results.append({
                "timeframe": "1day", "date": day.isoformat(),
                "status": "error", "rows": 0, "violations": [str(exc)],
            })

    # ---- minute family --------------------------------------------------- #
    if need_minute:
        try:
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE raw AS {_load_raw_sql(config, SOURCE_DATASET['1min'], day)}"
            )
            con.execute(f"CREATE OR REPLACE TEMP TABLE src_min AS {_minute_source_sql()}")
            valid_min, unique_min = con.execute(
                f"""SELECT count(*), (SELECT count(*) FROM (
                        SELECT DISTINCT ticker, make_timestamp(window_start // 1000)
                        FROM raw WHERE {MINUTE_VALID}))
                    FROM raw WHERE {MINUTE_VALID}"""
            ).fetchone()
            minute_collisions = int(valid_min - unique_min)

            if rebuild or day not in skip.get("1min", set()):
                if "1min" in want:
                    con.execute("CREATE OR REPLACE TEMP TABLE out_1min AS SELECT * FROM src_min")
                    res = build_one_partition(
                        con, config, "1min", day, "out_1min", unique_min,
                        compression, row_group_size,
                    )
                    res["collisions"] = minute_collisions
                    results.append(res)

            for tf in ("5min", "15min"):
                if tf not in want:
                    continue
                minutes = RESAMPLE_MINUTES[tf]
                con.execute(
                    f"CREATE OR REPLACE TEMP TABLE out_{tf} AS {_resample_sql(minutes)}"
                )
                expected = con.execute(
                    f"SELECT count(*) FROM (SELECT DISTINCT ticker, "
                    f"time_bucket(INTERVAL '{minutes} minutes', ts) FROM src_min)"
                ).fetchone()[0]
                res = build_one_partition(
                    con, config, tf, day, f"out_{tf}", expected,
                    compression, row_group_size,
                )
                # Extra invariant: resampled volume must equal the minute total.
                if res["status"] == "written":
                    sums = con.execute(
                        f"SELECT (SELECT COALESCE(sum(volume),0) FROM out_{tf}), "
                        f"(SELECT COALESCE(sum(volume),0) FROM src_min)"
                    ).fetchone()
                    if sums[0] != sums[1]:
                        res = {
                            "timeframe": tf, "date": day.isoformat(),
                            "status": "reconcile_failed", "rows": res["rows"],
                            "violations": [f"volume-sum: {sums[0]} != minute {sums[1]}"],
                        }
                results.append(res)
        except Exception as exc:  # noqa: BLE001
            for tf in want:
                if tf in ("1min", "5min", "15min"):
                    results.append({
                        "timeframe": tf, "date": day.isoformat(),
                        "status": "error", "rows": 0, "violations": [str(exc)],
                    })

    return results


# --------------------------------------------------------------------------- #
# Post-build: _meta.json, split-flag cross-check, report
# --------------------------------------------------------------------------- #

def write_meta(con, s3, config: MinioConfig, timeframe: str) -> dict:
    """Compute and upload ``bars/tf=<tf>/_meta.json``; return the meta dict."""
    dates = sorted(discover_existing_partitions(s3, config.bucket, timeframe))
    glob_uri = minio_io.s3_uri(config, minio_io.timeframe_glob(timeframe))
    total_rows = 0
    if dates:
        total_rows = con.execute(
            f"SELECT COALESCE(sum(num_rows), 0) FROM parquet_file_metadata('{glob_uri}')"
        ).fetchone()[0]

    derivation = {
        "1day": "Official daily aggregates (day_aggs_v1). NOT resampled from minutes.",
        "1min": "1:1 passthrough of minute_aggs_v1.",
        "5min": "Resampled from raw 1min (open=first, high=max, low=min, close=last, "
                "volume=sum, transactions=sum).",
        "15min": "Resampled from raw 1min (open=first, high=max, low=min, close=last, "
                 "volume=sum, transactions=sum).",
    }[timeframe]

    meta = {
        "timeframe": timeframe,
        "layout": f"{minio_io.BARS_PREFIX}/tf={timeframe}/date=YYYY-MM-DD/part.parquet",
        "schema": {col: BAR_COLUMN_TYPES[col] for col in BAR_COLUMNS},
        "sort_order": "(ticker, ts) within each date partition",
        "timezone": "UTC (ts is timezone-naive and semantically UTC)",
        "ts_convention": TS_CONVENTION[timeframe],
        "date_partition_meaning": (
            "ET trading session date, taken from the source csv.gz filename"
        ),
        "adjustment": "RAW, split-UNADJUSTED and dividend-UNADJUSTED. "
                      "Adjustment is applied at query time by marketdata.reader.",
        "dedup_policy": (
            "Unique (ticker, ts) guaranteed. When two securities share one "
            "ticker on the same key (recycled/colliding symbols), the highest-"
            "volume print is kept (tie: most transactions, then highest close). "
            "Collisions are counted in the build report."
        ),
        "source_dataset": SOURCE_DATASET[timeframe],
        "derivation": derivation,
        "raw_window_start_unit": WINDOW_START_UNIT,
        "date_range": {
            "first": dates[0].isoformat() if dates else None,
            "last": dates[-1].isoformat() if dates else None,
        },
        "partition_count": len(dates),
        "total_rows": int(total_rows),
        "compression": DEFAULT_COMPRESSION,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    s3.put_object(
        Bucket=config.bucket,
        Key=minio_io.meta_key(timeframe),
        Body=json.dumps(meta, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return meta


def split_flag_crosscheck(con, config: MinioConfig) -> dict:
    """Flag daily moves > 40% (probable unadjusted splits) and cross-check splits.

    These large raw moves are EXPECTED in raw storage -- the point is to confirm
    they line up with known splits, not to fix them. Returns a summary dict.
    """
    bars_uri = minio_io.s3_uri(config, minio_io.timeframe_glob("1day"))
    splits_uri = minio_io.s3_uri(config, f"{minio_io.REFERENCE_PREFIX}/splits.parquet")
    try:
        rows = con.execute(
            f"""
            WITH d AS (
                SELECT ticker, CAST(ts AS DATE) AS dt, close,
                       lag(close) OVER (PARTITION BY ticker ORDER BY ts) AS prev_close
                FROM read_parquet('{bars_uri}')
            ),
            moves AS (
                SELECT ticker, dt, close / prev_close - 1.0 AS ret
                FROM d
                WHERE prev_close IS NOT NULL AND prev_close > 0
            ),
            flagged AS (
                SELECT m.ticker, m.dt, m.ret,
                       (s.ticker IS NOT NULL) AS has_split,
                       s.split_from, s.split_to
                FROM moves m
                LEFT JOIN read_parquet('{splits_uri}') s
                       ON s.ticker = m.ticker AND s.execution_date = m.dt
                WHERE abs(m.ret) > 0.40
            )
            SELECT
                count(*) AS flagged,
                count(*) FILTER (WHERE has_split) AS matched,
                count(*) FILTER (WHERE NOT has_split) AS unmatched
            FROM flagged
            """
        ).fetchone()
        examples = con.execute(
            f"""
            WITH d AS (
                SELECT ticker, CAST(ts AS DATE) AS dt, close,
                       lag(close) OVER (PARTITION BY ticker ORDER BY ts) AS prev_close
                FROM read_parquet('{bars_uri}')
            ),
            moves AS (
                SELECT ticker, dt, close / prev_close - 1.0 AS ret
                FROM d WHERE prev_close IS NOT NULL AND prev_close > 0
            )
            SELECT m.ticker, m.dt::VARCHAR, round(m.ret, 4),
                   (s.ticker IS NOT NULL) AS has_split,
                   s.split_from, s.split_to
            FROM moves m
            LEFT JOIN read_parquet('{splits_uri}') s
                   ON s.ticker = m.ticker AND s.execution_date = m.dt
            WHERE abs(m.ret) > 0.40
            ORDER BY abs(m.ret) DESC
            LIMIT 15
            """
        ).fetchall()
        return {
            "threshold": 0.40,
            "flagged": int(rows[0]),
            "matched_to_split": int(rows[1]),
            "unmatched": int(rows[2]),
            "note": "Large raw moves are EXPECTED (unadjusted storage). 'matched' "
                    "means a split exists on that exact date; 'unmatched' moves are "
                    "candidates to inspect (real crashes, low-priced names, or "
                    "splits missing from reference).",
            "top_examples": [
                {
                    "ticker": e[0], "date": e[1], "return": e[2],
                    "has_split": bool(e[3]), "split_from": e[4], "split_to": e[5],
                }
                for e in examples
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the canonical bars/ corpus in MinIO from raw csv.gz.",
    )
    parser.add_argument(
        "--timeframes", nargs="+", default=list(TIMEFRAMES), choices=list(TIMEFRAMES),
        help="Timeframes to build. Default: all four.",
    )
    parser.add_argument("--start", type=parse_date, default=None,
                        help="Inclusive start date YYYY-MM-DD. Default: earliest in archive.")
    parser.add_argument("--end", type=parse_date, default=None,
                        help="Inclusive end date YYYY-MM-DD. Default: latest in archive.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Rebuild partitions even if already present.")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel date workers (each its own DuckDB connection). Default: 8.")
    parser.add_argument("--memory-limit", default=None,
                        help="Optional DuckDB memory limit per worker, e.g. 4GB.")
    parser.add_argument("--compression", default=DEFAULT_COMPRESSION,
                        choices=["zstd", "snappy", "gzip", "uncompressed"])
    parser.add_argument("--row-group-size", type=int, default=DEFAULT_ROW_GROUP_SIZE)
    parser.add_argument("--no-meta", action="store_true",
                        help="Skip recomputing _meta.json after the build.")
    parser.add_argument("--no-report", action="store_true",
                        help="Skip the split-flag cross-check and build report.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the work (dates per timeframe) without building.")
    args = parser.parse_args(argv)

    config = minio_io.resolve_minio_config()
    s3 = minio_io.make_s3_client(config, max_pool=args.workers + 8)

    print(f"MinIO endpoint: {config.endpoint_url}")
    print(f"MinIO bucket:   {config.bucket}")
    print(f"Timeframes:     {', '.join(args.timeframes)}")

    # Source dates = union of the dates available for each needed dataset.
    needed_datasets = {SOURCE_DATASET[tf] for tf in args.timeframes}
    source_dates: dict[str, dict[date, int]] = {}
    for ds in needed_datasets:
        source_dates[ds] = discover_source_dates(s3, config.bucket, ds, args.start, args.end)
        if source_dates[ds]:
            ds_dates = sorted(source_dates[ds])
            print(f"Raw {ds}: {len(ds_dates):,} dates ({ds_dates[0]} .. {ds_dates[-1]})")
        else:
            print(f"Raw {ds}: no dates found in range", file=sys.stderr)

    # Existing partitions per timeframe (for the idempotent skip).
    skip: dict[str, set[date]] = {}
    for tf in args.timeframes:
        skip[tf] = discover_existing_partitions(s3, config.bucket, tf)

    # The set of dates to iterate = union of source dates for requested datasets.
    all_dates: set[date] = set()
    for ds in needed_datasets:
        all_dates |= set(source_dates[ds].keys())
    work_dates = sorted(all_dates)

    # Count how many (tf, date) partitions actually need building.
    todo = 0
    for d in work_dates:
        for tf in args.timeframes:
            if d in source_dates[SOURCE_DATASET[tf]] and (args.rebuild or d not in skip[tf]):
                todo += 1
    print(f"Dates in range: {len(work_dates):,}  |  partitions to build: {todo:,}  "
          f"(rebuild={args.rebuild})")

    if args.dry_run:
        for tf in args.timeframes:
            present = len(skip[tf])
            avail = len(source_dates[SOURCE_DATASET[tf]])
            print(f"  [dry-run] {tf}: {avail:,} source dates, {present:,} already built")
        return 0

    started = time.time()
    all_results: list[dict] = []
    done = 0

    def task(day: date) -> list[dict]:
        tfs_for_day = [
            tf for tf in args.timeframes
            if day in source_dates[SOURCE_DATASET[tf]]
        ]
        return build_date(
            config=config, day=day, timeframes=tfs_for_day, skip=skip,
            rebuild=args.rebuild, compression=args.compression,
            row_group_size=args.row_group_size, memory_limit=args.memory_limit,
        )

    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_date = {pool.submit(task, d): d for d in work_dates}
        for fut in futures.as_completed(future_to_date):
            d = future_to_date[fut]
            done += 1
            try:
                all_results.extend(fut.result())
            except Exception as exc:  # noqa: BLE001
                all_results.append({
                    "timeframe": "?", "date": d.isoformat(),
                    "status": "error", "rows": 0, "violations": [str(exc)],
                })
            if done % 100 == 0 or done == len(work_dates):
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                print(f"  {done:,}/{len(work_dates):,} dates  "
                      f"({rate:.1f} dates/s, {elapsed:.0f}s)", flush=True)

    # ---- summarise ------------------------------------------------------- #
    by_status: dict[str, int] = {}
    failures: list[dict] = []
    rows_written: dict[str, int] = {tf: 0 for tf in args.timeframes}
    written_dates: dict[str, list[str]] = {tf: [] for tf in args.timeframes}
    collisions: dict[str, int] = {tf: 0 for tf in args.timeframes}
    for r in all_results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        collisions[r["timeframe"]] = collisions.get(r["timeframe"], 0) + r.get("collisions", 0)
        if r["status"] == "written":
            rows_written[r["timeframe"]] = rows_written.get(r["timeframe"], 0) + r["rows"]
            written_dates.setdefault(r["timeframe"], []).append(r["date"])
        elif r["status"] != "skipped":
            failures.append(r)

    print("\n=== build summary ===")
    for status, count in sorted(by_status.items()):
        print(f"  {status}: {count:,}")
    for tf in args.timeframes:
        wd = sorted(written_dates.get(tf, []))
        rng = f"{wd[0]} .. {wd[-1]}" if wd else "-"
        coll = collisions.get(tf, 0)
        coll_note = f", {coll:,} collision rows deduped" if coll else ""
        print(f"  {tf}: wrote {len(wd):,} partitions, {rows_written.get(tf,0):,} rows ({rng}){coll_note}")

    # ---- _meta.json ------------------------------------------------------ #
    meta_con = minio_io.connect_duckdb(config, threads=4)
    if args.memory_limit:
        meta_con.execute(f"PRAGMA memory_limit='{args.memory_limit}'")
    metas: dict[str, dict] = {}
    if not args.no_meta:
        print("\nWriting _meta.json ...")
        for tf in args.timeframes:
            metas[tf] = write_meta(meta_con, s3, config, tf)
            print(f"  {tf}: {metas[tf]['partition_count']:,} partitions, "
                  f"{metas[tf]['total_rows']:,} rows")

    # ---- split-flag cross-check (daily only) ----------------------------- #
    crosscheck: dict = {}
    if not args.no_report and "1day" in args.timeframes:
        print("\nDaily split-flag cross-check (raw moves > 40%) ...")
        crosscheck = split_flag_crosscheck(meta_con, config)
        if "error" in crosscheck:
            print(f"  cross-check error: {crosscheck['error']}")
        else:
            print(f"  flagged={crosscheck['flagged']:,}  "
                  f"matched_to_split={crosscheck['matched_to_split']:,}  "
                  f"unmatched={crosscheck['unmatched']:,}")
    meta_con.close()

    # ---- build report ---------------------------------------------------- #
    report = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": config.endpoint_url,
        "bucket": config.bucket,
        "timeframes": args.timeframes,
        "range": {
            "start": args.start.isoformat() if args.start else None,
            "end": args.end.isoformat() if args.end else None,
        },
        "status_counts": by_status,
        "rows_written": rows_written,
        "partitions_written": {tf: len(written_dates.get(tf, [])) for tf in args.timeframes},
        "collision_rows_deduped": collisions,
        "collision_note": (
            "Rows dropped because two securities shared one ticker on the same "
            "(ticker, ts); the highest-volume print was kept. See DEDUP_ORDER."
        ),
        "validation_failures": failures,
        "split_flag_crosscheck": crosscheck,
        "meta": metas,
    }
    if not args.no_report:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = _AUDIT_DIR / f"bars_build_report_{stamp}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        s3.put_object(
            Bucket=config.bucket,
            Key=f"{minio_io.BARS_PREFIX}/_build_report.json",
            Body=json.dumps(report, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        print(f"\nBuild report: {report_path}")
        print(f"             s3://{config.bucket}/{minio_io.BARS_PREFIX}/_build_report.json")

    if failures:
        print(f"\n{len(failures)} partition(s) failed validation/build. See report.",
              file=sys.stderr)
        return 1
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
