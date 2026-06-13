#!/usr/bin/env python3
"""
Standalone daily pipeline: pull Massive.com stock flatfiles, post-process into
per-ticker Parquet, and upload everything to MinIO. Designed to run unattended
under Dagster on a daily schedule, and to self-heal by backfilling any missing
trading days within a lookback window.

Trading-day source: Massive's own S3 listing. Massive only publishes flat files
on trading days, so any date present in their bucket is by definition a trading
day. No external calendar dependency.

Pipeline (per dataset, per trading date that needs work):
  1. Download raw {dataset}/YYYY/MM/YYYY-MM-DD.csv.gz from Massive
  2. Upload raw .csv.gz to MinIO at the mirrored key
  3. Postprocess into per-ticker Parquet:
       - day_aggs_v1   -> daily/{TICKER}.parquet
       - minute_aggs_v1 -> 1min/{TICKER}.parquet
                        -> 5min/{TICKER}.parquet  (derived from 1min)
                        -> 15min/{TICKER}.parquet (derived from 1min)
     Per-ticker merge is download-merge-upload via DuckDB, deduped on
     (ticker, window_start). Only tickers that appear in the new day are touched.
  4. Advance meta/_pivot_checkpoint.txt when every dataset in this run finished
     pivot (and resample, if enabled) with zero ticker failures

A date is (re)synced when any of: --force; raw missing or size mismatch vs
Massive; or the date is after meta/_pivot_checkpoint.txt (re-pivot after a
prior crash that uploaded raw but never finished pivot). Raw re-upload is
idempotent and cheap.

Required env vars (or pass via flags):
  MASSIVE_S3_ACCESS_KEY_ID
  MASSIVE_S3_SECRET_ACCESS_KEY
  MINIO_ENDPOINT_URL          (default: http://s3-lan.ekenhome.se:9000)
  MINIO_ACCESS_KEY_ID         (default: stocks)
  MINIO_SECRET_ACCESS_KEY
  MINIO_BUCKET                (default: stocks-us)

Examples:
  # Normal daily run: gap-scan last 7 trading days, fill anything missing.
  python tools/daily_massive_to_minio.py

  # Explicit backfill window (ignores lookback).
  python tools/daily_massive_to_minio.py --start 2026-04-01 --end 2026-04-30

  # Daily-bars only, skip minute pipeline (fast smoke test).
  python tools/daily_massive_to_minio.py --datasets day_aggs_v1

  # Force re-process even if MinIO already has the raw file.
  python tools/daily_massive_to_minio.py --force

  # Dry run: report what would happen, touch nothing.
  python tools/daily_massive_to_minio.py --dry-run
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import boto3
import duckdb
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()


MASSIVE_ENDPOINT = "https://files.massive.com"
MASSIVE_BUCKET = "flatfiles"

DATASET_PREFIXES = {
    "day_aggs_v1": "us_stocks_sip/day_aggs_v1",
    "minute_aggs_v1": "us_stocks_sip/minute_aggs_v1",
}

DATASET_TO_TIMEFRAME_DIR = {
    "day_aggs_v1": "daily",
    "minute_aggs_v1": "1min",
}

RESAMPLE_INTERVALS_MINUTES = [5, 15]

CSV_COLUMNS_SQL = """
{
    'ticker': 'VARCHAR',
    'volume': 'DOUBLE',
    'open': 'DOUBLE',
    'close': 'DOUBLE',
    'high': 'DOUBLE',
    'low': 'DOUBLE',
    'window_start': 'BIGINT',
    'transactions': 'DOUBLE'
}
"""

WINDOW_START_NS_THRESHOLD = 10**15

DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\.csv\.gz$")

CHECKPOINT_KEY = "meta/_pivot_checkpoint.txt"


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def path_for_duckdb(path: Path) -> str:
    return path.resolve().as_posix()


def safe_filename_for_ticker(ticker: str) -> str:
    safe = re.sub(r"[^A-Z0-9._-]+", "_", ticker.upper())
    safe = safe.strip("._-")
    return safe or "UNKNOWN"


def parse_yyyy_mm_dd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def object_date_from_key(key: str) -> date | None:
    match = DATE_RE.search(key)
    if not match:
        return None
    return parse_yyyy_mm_dd(match.group("date"))


def month_starts(start: date, end: date) -> Iterable[date]:
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while current <= last:
        yield current
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def make_s3_client(
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    max_pool: int = 64,
):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 10, "mode": "standard"},
            max_pool_connections=max_pool,
            s3={"addressing_style": "path"},
        ),
    )


def list_dates_in_prefix(
    s3,
    bucket: str,
    base_prefix: str,
    start: date,
    end: date,
) -> dict[date, S3Object]:
    """Return {date: S3Object} for every YYYY-MM-DD.csv.gz key found in range."""
    found: dict[date, S3Object] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for m in month_starts(start, end):
        prefix = f"{base_prefix}/{m.year:04d}/{m.month:02d}/"
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                d = object_date_from_key(key)
                if d is None or not (start <= d <= end):
                    continue
                found[d] = S3Object(key=key, size=int(obj["Size"]))
    return found


def minio_key_for_raw(dataset: str, d: date) -> str:
    return f"{dataset}/{d.year:04d}/{d.month:02d}/{d.isoformat()}.csv.gz"


def download_s3_object(s3, bucket: str, key: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    transfer_config = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )
    s3.download_file(Bucket=bucket, Key=key, Filename=str(tmp), Config=transfer_config)
    tmp.replace(dest)


def upload_s3_object(s3, bucket: str, key: str, src: Path) -> None:
    transfer_config = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )
    s3.upload_file(Filename=str(src), Bucket=bucket, Key=key, Config=transfer_config)


def read_checkpoint(s3, bucket: str) -> date | None:
    try:
        obj = s3.get_object(Bucket=bucket, Key=CHECKPOINT_KEY)
        text = obj["Body"].read().decode("utf-8").strip()
        return parse_yyyy_mm_dd(text) if text else None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise


def write_checkpoint(s3, bucket: str, d: date) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=CHECKPOINT_KEY,
        Body=d.isoformat().encode("utf-8"),
        ContentType="text/plain",
    )


def configure_duckdb(threads: int = 1) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"PRAGMA threads={threads}")
    return con


def load_new_day_into_table(
    con: duckdb.DuckDBPyConnection,
    csv_gz_paths: list[Path],
    table_name: str,
) -> None:
    """
    Load one or more day_aggs / minute_aggs .csv.gz files into a DuckDB table,
    deriving trading_date from the filename.
    """
    con.execute(f"DROP TABLE IF EXISTS {table_name}")
    files_sql = "[" + ", ".join(sql_quote(path_for_duckdb(p)) for p in csv_gz_paths) + "]"
    con.execute(
        f"""
        CREATE TABLE {table_name} AS
        SELECT
            upper(ticker) AS ticker,
            CAST(COALESCE(volume, 0) AS BIGINT) AS volume,
            CAST(open AS DOUBLE) AS open,
            CAST(close AS DOUBLE) AS close,
            CAST(high AS DOUBLE) AS high,
            CAST(low AS DOUBLE) AS low,
            CAST(window_start AS BIGINT) AS window_start,
            CAST(COALESCE(transactions, 0) AS BIGINT) AS transactions,
            CAST(
                regexp_extract(
                    filename,
                    '([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})\\.csv\\.gz$',
                    1
                ) AS DATE
            ) AS trading_date
        FROM read_csv(
            {files_sql},
            header = true,
            compression = 'gzip',
            filename = true,
            columns = {CSV_COLUMNS_SQL}
        )
        WHERE ticker IS NOT NULL
          AND window_start IS NOT NULL
          AND open IS NOT NULL
          AND high IS NOT NULL
          AND low IS NOT NULL
          AND close IS NOT NULL
        """
    )


def get_tickers_in_table(con: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    rows = con.execute(
        f"SELECT ticker FROM {table_name} GROUP BY ticker ORDER BY ticker"
    ).fetchall()
    return [row[0] for row in rows]


def detect_window_unit(con: duckdb.DuckDBPyConnection, sample_path: Path) -> str:
    row = con.execute(
        f"""
        SELECT MAX(window_start) AS mx
        FROM read_parquet({sql_quote(path_for_duckdb(sample_path))})
        WHERE window_start IS NOT NULL
        """
    ).fetchone()
    mx = row[0]
    if mx is None:
        return "ns"
    return "ns" if mx > WINDOW_START_NS_THRESHOLD else "ms"


def resample_one_ticker(
    con: duckdb.DuckDBPyConnection,
    in_path: Path,
    out_path: Path,
    interval_minutes: int,
    window_unit: str,
    compression: str,
    row_group_size: int,
) -> int:
    sec = interval_minutes * 60
    bar = sec * (1_000_000_000 if window_unit == "ns" else 1_000)
    in_q = sql_quote(path_for_duckdb(in_path))
    out_q = sql_quote(path_for_duckdb(out_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"""
        COPY (
            WITH bucketed AS (
                SELECT
                    *,
                    CAST(FLOOR(CAST(window_start AS DOUBLE) / CAST({bar} AS DOUBLE))
                         AS BIGINT) AS bucket_id
                FROM read_parquet({in_q})
                WHERE window_start IS NOT NULL
            )
            SELECT
                ticker,
                CAST(SUM(volume) AS BIGINT) AS volume,
                CAST(first(open ORDER BY window_start ASC) AS DOUBLE) AS open,
                CAST(last(close ORDER BY window_start ASC) AS DOUBLE) AS close,
                CAST(MAX(high) AS DOUBLE) AS high,
                CAST(MIN(low) AS DOUBLE) AS low,
                CAST(bucket_id * CAST({bar} AS BIGINT) AS BIGINT) AS window_start,
                CAST(SUM(transactions) AS BIGINT) AS transactions,
                CAST(arg_min(trading_date, window_start) AS DATE) AS trading_date
            FROM bucketed
            GROUP BY ticker, bucket_id
            ORDER BY window_start
        )
        TO {out_q}
        (FORMAT PARQUET, COMPRESSION {compression.upper()}, ROW_GROUP_SIZE {row_group_size})
        """
    )
    return con.execute(
        f"SELECT COUNT(*) FROM read_parquet({sql_quote(path_for_duckdb(out_path))})"
    ).fetchone()[0]


def select_dates_to_sync(
    candidates: list[date],
    massive_objects: dict[date, S3Object],
    minio_objects: dict[date, S3Object],
    *,
    checkpoint: date | None,
    force: bool,
) -> list[date]:
    """
    Dates to download from Massive and feed into pivot.

    After checkpoint, always re-sync (raw upload is idempotent) so a prior run
    that uploaded raw but crashed during pivot will self-heal. At or before
    checkpoint, only sync when raw is missing or its size differs from Massive.
    """
    to_process: list[date] = []
    for d in candidates:
        if force:
            to_process.append(d)
            continue
        if checkpoint is None or d > checkpoint:
            to_process.append(d)
            continue
        existing = minio_objects.get(d)
        if existing is None or existing.size != massive_objects[d].size:
            to_process.append(d)
    return to_process


def sync_raw_flatfiles(
    massive_s3,
    minio_s3,
    minio_bucket: str,
    datasets: list[str],
    start: date,
    end: date,
    workdir: Path,
    force: bool,
    dry_run: bool,
    checkpoint: date | None,
) -> dict[str, list[tuple[date, Path]]]:
    """
    For each dataset, find trading dates that need work, download from Massive,
    upload to MinIO, and return local .csv.gz paths for downstream pivot.

    Selection uses the pivot checkpoint (not raw presence alone) so a partial
    run that uploaded raw but failed during pivot will be retried.
    """
    synced: dict[str, list[tuple[date, Path]]] = {ds: [] for ds in datasets}

    for dataset in datasets:
        print(f"\n=== Sync raw: {dataset} ({start} .. {end}) ===")
        if checkpoint is None:
            print("  Pivot checkpoint: (none — all dates in window are candidates)")
        else:
            print(f"  Pivot checkpoint: {checkpoint} "
                  f"(dates after this are re-synced for pivot even if raw exists)")
        massive_prefix = DATASET_PREFIXES[dataset]
        massive_objects = list_dates_in_prefix(
            massive_s3, MASSIVE_BUCKET, massive_prefix, start, end
        )
        if not massive_objects:
            print(f"  Massive has no objects in range for {dataset}; skipping.")
            continue

        minio_objects = list_dates_in_prefix(
            minio_s3, minio_bucket, dataset, start, end
        )

        candidates = sorted(massive_objects.keys())
        to_process = select_dates_to_sync(
            candidates,
            massive_objects,
            minio_objects,
            checkpoint=checkpoint,
            force=force,
        )

        print(
            f"  Massive dates: {len(massive_objects)}, "
            f"raw in MinIO: {len(minio_objects)}, "
            f"to sync+pivot: {len(to_process)}"
        )

        if dry_run:
            for d in to_process:
                print(f"  [dry-run] would sync {massive_objects[d].key} "
                      f"({massive_objects[d].size:,} bytes)")
            continue

        for d in to_process:
            src_key = massive_objects[d].key
            dst_key = minio_key_for_raw(dataset, d)
            local_path = workdir / dataset / f"{d.isoformat()}.csv.gz"

            print(f"  {d}: download Massive -> {local_path.name}")
            started = time.time()
            download_s3_object(massive_s3, MASSIVE_BUCKET, src_key, local_path)
            dl_secs = time.time() - started

            print(f"  {d}: upload -> minio://{minio_bucket}/{dst_key}")
            started = time.time()
            upload_s3_object(minio_s3, minio_bucket, dst_key, local_path)
            up_secs = time.time() - started

            print(f"  {d}: ok (down {dl_secs:.1f}s, up {up_secs:.1f}s, "
                  f"{local_path.stat().st_size:,} bytes)")
            synced[dataset].append((d, local_path))

    return synced


def _write_new_rows_parquet(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    out_path: Path,
    compression: str,
) -> None:
    """
    Persist the in-memory new_rows table as a single Parquet on disk, sorted by
    ticker so that worker threads reading it with WHERE ticker = ? get row-group
    pruning via parquet statistics.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"""
        COPY (
            SELECT ticker, volume, open, close, high, low,
                   window_start, transactions, trading_date
            FROM {table_name}
            ORDER BY ticker, window_start
        )
        TO {sql_quote(path_for_duckdb(out_path))}
        (FORMAT PARQUET, COMPRESSION {compression.upper()})
        """
    )


def _merge_and_upload_one(
    ticker: str,
    new_rows_path: Path,
    timeframe_dir: str,
    download_dir: Path,
    out_dir: Path,
    minio_s3,
    minio_bucket: str,
    compression: str,
    row_group_size: int,
) -> tuple[str, str]:
    """
    Worker: download existing per-ticker parquet from MinIO (if any), merge with
    its slice of new_rows, upload merged parquet back to MinIO. Returns
    (ticker, status). Runs in a worker thread with its own DuckDB connection.
    """
    safe = safe_filename_for_ticker(ticker)
    key = f"{timeframe_dir}/{safe}.parquet"
    existing_local = download_dir / f"{safe}.parquet"
    out_local = out_dir / f"{safe}.parquet"

    existing_path: Path | None = None
    try:
        minio_s3.download_file(
            Bucket=minio_bucket, Key=key, Filename=str(existing_local)
        )
        existing_path = existing_local
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in ("404", "NoSuchKey", "NotFound"):
            raise

    con = duckdb.connect(database=":memory:")
    try:
        con.execute("SET preserve_insertion_order = false")

        new_select = f"""
            SELECT ticker, volume, open, close, high, low,
                   window_start, transactions, trading_date,
                   1 AS src_priority
            FROM read_parquet({sql_quote(path_for_duckdb(new_rows_path))})
            WHERE ticker = {sql_quote(ticker)}
        """
        if existing_path is not None:
            existing_select = f"""
            UNION ALL
            SELECT ticker, volume, open, close, high, low,
                   window_start, transactions, trading_date,
                   2 AS src_priority
            FROM read_parquet({sql_quote(path_for_duckdb(existing_path))})
            """
        else:
            existing_select = ""

        out_local.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"""
            COPY (
                WITH unioned AS (
                    {new_select}
                    {existing_select}
                ),
                ranked AS (
                    SELECT *,
                        row_number() OVER (
                            PARTITION BY ticker, window_start
                            ORDER BY src_priority ASC
                        ) AS rn
                    FROM unioned
                )
                SELECT ticker, volume, open, close, high, low,
                       window_start, transactions, trading_date
                FROM ranked
                WHERE rn = 1
                ORDER BY window_start
            )
            TO {sql_quote(path_for_duckdb(out_local))}
            (FORMAT PARQUET, COMPRESSION {compression.upper()},
             ROW_GROUP_SIZE {row_group_size})
            """
        )
    finally:
        con.close()

    upload_s3_object(minio_s3, minio_bucket, key, out_local)
    return ticker, ("merged" if existing_path is not None else "created")


def postprocess_pivot(
    minio_s3,
    minio_bucket: str,
    dataset: str,
    csv_gz_files: list[Path],
    label_dates: list[date],
    workdir: Path,
    compression: str,
    row_group_size: int,
    workers: int,
    max_ticker_failures: int = 0,
) -> tuple[list[str], list[tuple[str, str]]]:
    """
    Merge newly-synced .csv.gz rows into per-ticker parquet in MinIO using a
    fan-out worker pool. Returns (updated tickers, failed ticker errors).
    """
    if not csv_gz_files:
        print(f"\n=== Pivot: {dataset}: nothing to do ===")
        return [], []

    timeframe_dir = DATASET_TO_TIMEFRAME_DIR[dataset]
    print(
        f"\n=== Pivot: {dataset} -> {timeframe_dir}/ "
        f"({len(csv_gz_files)} date(s): {min(label_dates)} .. {max(label_dates)}) ==="
    )

    main_con = configure_duckdb(threads=2)
    try:
        load_new_day_into_table(main_con, csv_gz_files, "new_rows")
        new_row_count = main_con.execute("SELECT COUNT(*) FROM new_rows").fetchone()[0]
        tickers = get_tickers_in_table(main_con, "new_rows")
        print(f"  Loaded {new_row_count:,} new rows across {len(tickers):,} tickers")

        new_rows_path = workdir / f"{timeframe_dir}_new_rows.parquet"
        _write_new_rows_parquet(main_con, "new_rows", new_rows_path, compression)
        print(f"  Staged sorted new_rows -> {new_rows_path.name} "
              f"({new_rows_path.stat().st_size / 1024 / 1024:.1f} MiB)")
    finally:
        main_con.close()

    download_dir = workdir / f"{timeframe_dir}_existing"
    download_dir.mkdir(parents=True, exist_ok=True)
    out_dir = workdir / f"{timeframe_dir}_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Fanning out merge+upload across {workers} workers...")
    updated: list[str] = []
    failed: list[tuple[str, str]] = []
    started = time.time()

    def task(ticker: str) -> tuple[str, str]:
        return _merge_and_upload_one(
            ticker=ticker,
            new_rows_path=new_rows_path,
            timeframe_dir=timeframe_dir,
            download_dir=download_dir,
            out_dir=out_dir,
            minio_s3=minio_s3,
            minio_bucket=minio_bucket,
            compression=compression,
            row_group_size=row_group_size,
        )

    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_ticker = {ex.submit(task, t): t for t in tickers}
        done = 0
        for fut in futures.as_completed(future_to_ticker):
            ticker = future_to_ticker[fut]
            done += 1
            try:
                t, _status = fut.result()
                updated.append(t)
            except Exception as exc:
                failed.append((ticker, str(exc)))
                print(f"    FAILED {ticker}: {exc}", file=sys.stderr)
            if done % 1000 == 0 or done == len(tickers):
                elapsed = time.time() - started
                rate = done / elapsed if elapsed > 0 else 0
                print(f"    {done:,}/{len(tickers):,}  "
                      f"({rate:.0f} tickers/s, {elapsed:.0f}s elapsed)")

    if failed:
        if len(failed) > max_ticker_failures:
            raise RuntimeError(
                f"{len(failed)} ticker(s) failed during pivot for {dataset}: "
                f"{failed[:5]}{'...' if len(failed) > 5 else ''}"
            )
        print(
            f"  WARNING: {len(failed)} ticker(s) failed during pivot for {dataset} "
            f"(budget {max_ticker_failures}); continuing.",
            file=sys.stderr,
        )

    return updated, failed


def _resample_and_upload_one(
    ticker: str,
    in_path: Path,
    intervals: list[int],
    window_unit: str,
    out_dirs: dict[int, Path],
    minio_s3,
    minio_bucket: str,
    compression: str,
    row_group_size: int,
) -> tuple[str, list[int]]:
    """
    Worker: for one ticker, read its locally-staged 1min parquet, derive each
    requested resample interval, upload each derivative to MinIO. Returns
    (ticker, list_of_intervals_that_were_written).
    """
    safe = safe_filename_for_ticker(ticker)
    con = duckdb.connect(database=":memory:")
    written: list[int] = []
    try:
        con.execute("SET preserve_insertion_order = false")
        for interval in intervals:
            out_path = out_dirs[interval] / f"{safe}.parquet"
            resample_one_ticker(
                con=con,
                in_path=in_path,
                out_path=out_path,
                interval_minutes=interval,
                window_unit=window_unit,
                compression=compression,
                row_group_size=row_group_size,
            )
            key = f"{interval}min/{safe}.parquet"
            upload_s3_object(minio_s3, minio_bucket, key, out_path)
            written.append(interval)
    finally:
        con.close()
    return ticker, written


def postprocess_resample(
    minio_s3,
    minio_bucket: str,
    updated_tickers: list[str],
    workdir: Path,
    compression: str,
    row_group_size: int,
    workers: int,
    max_ticker_failures: int = 0,
) -> list[tuple[str, str]]:
    """
    For each ticker whose 1min file changed, regenerate its 5min and 15min
    derivatives from the freshly-merged 1min parquet that was just uploaded.
    Returns failed (ticker, error) pairs.
    """
    if not updated_tickers:
        print("\n=== Resample: nothing to do ===")
        return []

    src_dir = workdir / "1min_out"
    intervals = RESAMPLE_INTERVALS_MINUTES
    print(
        f"\n=== Resample: 1min -> {', '.join(f'{m}min' for m in intervals)} "
        f"({len(updated_tickers):,} tickers, {workers} workers) ==="
    )

    sample = src_dir / f"{safe_filename_for_ticker(updated_tickers[0])}.parquet"
    if not sample.exists():
        print(f"  No 1min source found at {sample}; skipping resample.")
        return []

    probe_con = configure_duckdb(threads=1)
    try:
        unit = detect_window_unit(probe_con, sample)
    finally:
        probe_con.close()
    print(f"  window_start unit (auto): {unit}")

    out_dirs: dict[int, Path] = {}
    for interval in intervals:
        d = workdir / f"{interval}min_out"
        d.mkdir(parents=True, exist_ok=True)
        out_dirs[interval] = d

    eligible: list[str] = []
    for ticker in updated_tickers:
        if (src_dir / f"{safe_filename_for_ticker(ticker)}.parquet").exists():
            eligible.append(ticker)
    print(f"  Eligible (have local 1min source): {len(eligible):,}")

    failed: list[tuple[str, str]] = []
    started = time.time()

    def task(ticker: str) -> tuple[str, list[int]]:
        return _resample_and_upload_one(
            ticker=ticker,
            in_path=src_dir / f"{safe_filename_for_ticker(ticker)}.parquet",
            intervals=intervals,
            window_unit=unit,
            out_dirs=out_dirs,
            minio_s3=minio_s3,
            minio_bucket=minio_bucket,
            compression=compression,
            row_group_size=row_group_size,
        )

    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_ticker = {ex.submit(task, t): t for t in eligible}
        done = 0
        for fut in futures.as_completed(future_to_ticker):
            ticker = future_to_ticker[fut]
            done += 1
            try:
                fut.result()
            except Exception as exc:
                failed.append((ticker, str(exc)))
                print(f"    FAILED {ticker}: {exc}", file=sys.stderr)
            if done % 1000 == 0 or done == len(eligible):
                elapsed = time.time() - started
                rate = done / elapsed if elapsed > 0 else 0
                print(f"    {done:,}/{len(eligible):,}  "
                      f"({rate:.0f} tickers/s, {elapsed:.0f}s elapsed)")

    if failed:
        if len(failed) > max_ticker_failures:
            raise RuntimeError(
                f"{len(failed)} ticker(s) failed during resample: "
                f"{failed[:5]}{'...' if len(failed) > 5 else ''}"
            )
        print(
            f"  WARNING: {len(failed)} ticker(s) failed during resample "
            f"(budget {max_ticker_failures}); continuing.",
            file=sys.stderr,
        )
    return failed


def compute_checkpoint_advance(
    current: date | None,
    dataset_max_dates: list[date],
) -> date | None:
    """
    New checkpoint high-water mark after a successful multi-dataset run.

    Uses the minimum of each dataset's max pivoted date so a day-only run
    cannot leapfrog a dataset that was not part of this invocation.
    """
    if not dataset_max_dates:
        return None
    candidate = min(dataset_max_dates)
    if current is None or candidate > current:
        return candidate
    return None


def compute_window(
    start: date | None,
    end: date | None,
    lookback_days: int,
) -> tuple[date, date]:
    today = date.today()
    if end is None:
        end = today
    if start is None:
        start = end - timedelta(days=lookback_days)
    if start > end:
        raise ValueError(f"--start ({start}) must be <= --end ({end})")
    return start, end


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Daily Massive -> MinIO flatfile pipeline (idempotent, self-healing).",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        choices=sorted(DATASET_PREFIXES),
        default=sorted(DATASET_PREFIXES),
        help="Which Massive flatfile datasets to sync. Default: all.",
    )
    parser.add_argument("--start", type=parse_yyyy_mm_dd, default=None,
                        help="Inclusive start date. Default: --end minus --lookback-days.")
    parser.add_argument("--end", type=parse_yyyy_mm_dd, default=None,
                        help="Inclusive end date. Default: today.")
    parser.add_argument("--lookback-days", type=int, default=7,
                        help="Calendar-day lookback when --start is not given. Default: 7.")
    parser.add_argument("--massive-access-key",
                        default=os.getenv("MASSIVE_S3_ACCESS_KEY_ID"))
    parser.add_argument("--massive-secret-key",
                        default=os.getenv("MASSIVE_S3_SECRET_ACCESS_KEY"))
    parser.add_argument("--minio-endpoint",
                        default=os.getenv("MINIO_ENDPOINT_URL",
                                          "http://s3-lan.ekenhome.se:9000"))
    parser.add_argument("--minio-access-key",
                        default=os.getenv("MINIO_ACCESS_KEY_ID", "stocks"))
    parser.add_argument("--minio-secret-key",
                        default=os.getenv("MINIO_SECRET_ACCESS_KEY"))
    parser.add_argument("--minio-bucket",
                        default=os.getenv("MINIO_BUCKET", "stocks-us"))
    parser.add_argument("--workers", type=int, default=16,
                        help="Worker threads for fan-out merge+upload (each gets its own "
                             "DuckDB connection). Default: 16.")
    parser.add_argument("--compression", default="zstd",
                        choices=["zstd", "snappy", "gzip", "uncompressed"])
    parser.add_argument("--row-group-size", type=int, default=122_880)
    parser.add_argument("--workdir", type=Path, default=None,
                        help="Local scratch dir. Default: a temp dir under the system tmp.")
    parser.add_argument("--keep-workdir", action="store_true",
                        help="Keep the local scratch dir after run (for debugging).")
    parser.add_argument("--force", action="store_true",
                        help="Re-sync and re-process even if MinIO already has the raw file.")
    parser.add_argument("--no-pivot", action="store_true",
                        help="Skip per-ticker postprocessing. Only sync raw .csv.gz.")
    parser.add_argument("--no-resample", action="store_true",
                        help="Skip 5min/15min resample step.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the work without downloading, processing, or uploading.")
    parser.add_argument("--max-ticker-failures", type=int, default=0,
                        help="Allow up to N per-ticker pivot/resample failures before "
                             "aborting (0 = strict). Checkpoint is not advanced while "
                             "any ticker failed, even within budget.")

    args = parser.parse_args(argv)

    if not args.massive_access_key or not args.massive_secret_key:
        print("Missing Massive credentials. Set MASSIVE_S3_ACCESS_KEY_ID / "
              "MASSIVE_S3_SECRET_ACCESS_KEY.", file=sys.stderr)
        return 2
    if not args.minio_secret_key:
        print("Missing MinIO secret. Set MINIO_SECRET_ACCESS_KEY.", file=sys.stderr)
        return 2

    start, end = compute_window(args.start, args.end, args.lookback_days)

    print(f"Massive endpoint: {MASSIVE_ENDPOINT}")
    print(f"MinIO endpoint:   {args.minio_endpoint}")
    print(f"MinIO bucket:     {args.minio_bucket}")
    print(f"Datasets:         {', '.join(args.datasets)}")
    print(f"Window:           {start} .. {end}")
    print(f"Force re-sync:    {args.force}")
    print(f"Dry run:          {args.dry_run}")
    print(f"Ticker fail budget:{args.max_ticker_failures}")

    massive_s3 = make_s3_client(
        MASSIVE_ENDPOINT, args.massive_access_key, args.massive_secret_key
    )
    minio_s3 = make_s3_client(
        args.minio_endpoint, args.minio_access_key, args.minio_secret_key
    )

    if args.workdir is not None:
        workdir = args.workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        cleanup_workdir = False
    else:
        workdir = Path(tempfile.mkdtemp(prefix="massive_to_minio_"))
        cleanup_workdir = not args.keep_workdir
    print(f"Workdir:          {workdir}")

    try:
        checkpoint = read_checkpoint(minio_s3, args.minio_bucket)
        if checkpoint is not None:
            print(f"Checkpoint (read): {checkpoint}")

        synced = sync_raw_flatfiles(
            massive_s3=massive_s3,
            minio_s3=minio_s3,
            minio_bucket=args.minio_bucket,
            datasets=args.datasets,
            start=start,
            end=end,
            workdir=workdir,
            force=args.force,
            dry_run=args.dry_run,
            checkpoint=checkpoint,
        )

        if args.dry_run:
            print("\n[dry-run] Skipping postprocessing.")
            return 0

        if args.no_pivot:
            print("\n--no-pivot set; skipping per-ticker postprocessing.")
        else:
            updated_1min: list[str] = []
            ticker_failures = 0
            dataset_max_dates: list[date] = []
            for dataset in args.datasets:
                synced_files = synced.get(dataset, [])
                if not synced_files:
                    continue
                csv_gz_files = [p for _, p in synced_files]
                label_dates = [d for d, _ in synced_files]
                updated, pivot_failed = postprocess_pivot(
                    minio_s3=minio_s3,
                    minio_bucket=args.minio_bucket,
                    dataset=dataset,
                    csv_gz_files=csv_gz_files,
                    label_dates=label_dates,
                    workdir=workdir,
                    compression=args.compression,
                    row_group_size=args.row_group_size,
                    workers=args.workers,
                    max_ticker_failures=args.max_ticker_failures,
                )
                ticker_failures += len(pivot_failed)
                dataset_max_dates.append(max(label_dates))
                if dataset == "minute_aggs_v1":
                    updated_1min = updated

            if not args.no_resample and updated_1min:
                resample_failed = postprocess_resample(
                    minio_s3=minio_s3,
                    minio_bucket=args.minio_bucket,
                    updated_tickers=updated_1min,
                    workdir=workdir,
                    compression=args.compression,
                    row_group_size=args.row_group_size,
                    workers=args.workers,
                    max_ticker_failures=args.max_ticker_failures,
                )
                ticker_failures += len(resample_failed)

            if ticker_failures:
                print(
                    f"\nNot advancing checkpoint: {ticker_failures} ticker failure(s) "
                    f"this run.",
                    file=sys.stderr,
                )
            elif set(args.datasets) != set(DATASET_PREFIXES):
                print(
                    "\nNot advancing checkpoint: --datasets is a subset of the "
                    "full pipeline (shared checkpoint is only updated when both "
                    "day_aggs_v1 and minute_aggs_v1 run).",
                )
            else:
                highest = compute_checkpoint_advance(checkpoint, dataset_max_dates)
                if highest is not None:
                    print(f"\nAdvancing checkpoint -> {highest}")
                    write_checkpoint(minio_s3, args.minio_bucket, highest)
                elif checkpoint is not None:
                    print(f"\nCheckpoint unchanged at {checkpoint}.")
                else:
                    print("\nNo checkpoint to advance (nothing pivoted).")

        print("\nDone.")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"Workdir retained: {workdir}")


if __name__ == "__main__":
    raise SystemExit(main())
