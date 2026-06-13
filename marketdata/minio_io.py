#!/usr/bin/env python3
"""
Shared MinIO/S3 and DuckDB plumbing for the canonical ``bars/`` corpus.

Everything that talks to MinIO -- the build script, the reference refresh, and
the read/adjust helper -- resolves credentials and constructs its clients
through this module so the layout, schema, and connection settings are defined
in exactly one place.

Credentials are read from the environment (a local ``.env`` is loaded if
present). Both naming conventions are accepted, in this order:

  endpoint : MINIO_ENDPOINT_URL  | MINIO_URL
  access   : MINIO_ACCESS_KEY_ID | MINIO_USERNAME
  secret   : MINIO_SECRET_ACCESS_KEY | MINIO_PASSWORD
  bucket   : MINIO_BUCKET
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import boto3
import duckdb
from botocore.config import Config
from dotenv import load_dotenv

# Load .env from the repo root (one level above this package) if present, then
# fall back to the default search so the module works from any CWD.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")
load_dotenv()


# --------------------------------------------------------------------------- #
# Layout + schema constants. These define the corpus contract; change with care.
# --------------------------------------------------------------------------- #

# The four canonical timeframes, in build/display order.
TIMEFRAMES: tuple[str, ...] = ("1day", "1min", "5min", "15min")

# Which raw flatfile dataset each timeframe is derived from.
#   1day  comes from the OFFICIAL daily aggregates (NOT resampled from minutes).
#   1min  is a 1:1 passthrough of the minute aggregates.
#   5min / 15min are RESAMPLED from the raw 1-minute bars (single source =
#   minute, so the intraday timeframes can never disagree with each other).
SOURCE_DATASET: dict[str, str] = {
    "1day": "day_aggs_v1",
    "1min": "minute_aggs_v1",
    "5min": "minute_aggs_v1",
    "15min": "minute_aggs_v1",
}

# Resample width in minutes for the derived intraday timeframes.
RESAMPLE_MINUTES: dict[str, int] = {"5min": 5, "15min": 15}

# Raw csv.gz key prefixes (the immutable cold archive -- never written by build).
RAW_PREFIX: dict[str, str] = {
    "day_aggs_v1": "day_aggs_v1",
    "minute_aggs_v1": "minute_aggs_v1",
}

# Root prefix for the built corpus and the reference tables.
BARS_PREFIX = "bars"
REFERENCE_PREFIX = "reference"

# window_start in the raw csv.gz is epoch NANOSECONDS (UTC). Verified against
# the source: a daily bar's window_start is midnight America/New_York expressed
# as a UTC instant (04:00 UTC in summer, 05:00 in winter).
WINDOW_START_UNIT = "nanoseconds"

# The one uniform schema for every partition of every timeframe. Column order is
# significant -- builds and the reader rely on it.
BAR_COLUMNS: tuple[str, ...] = (
    "ticker",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "transactions",
)

# DuckDB types for each bar column (used to document _meta and assert on read).
BAR_COLUMN_TYPES: dict[str, str] = {
    "ticker": "VARCHAR",
    "ts": "TIMESTAMP",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "volume": "BIGINT",
    "transactions": "BIGINT",
}

# Default Parquet write settings.
DEFAULT_COMPRESSION = "zstd"
DEFAULT_ROW_GROUP_SIZE = 122_880

# Per-timeframe human description of the ts convention, embedded in _meta.json.
TS_CONVENTION: dict[str, str] = {
    "1day": (
        "ts is midnight UTC (00:00:00) whose CALENDAR DATE is the ET trading "
        "session date, taken from the source filename -- NOT the ET session "
        "open and NOT the raw window_start (which is ET-midnight-as-UTC, i.e. "
        "04:00/05:00 UTC and wobbles with DST). Downstream must treat a daily "
        "bar as 'the entire ET session for that calendar date'. ts is "
        "timezone-naive and semantically UTC."
    ),
    "1min": (
        "ts is the true UTC wall-clock minute the bar STARTS (from "
        "window_start), so pre-market / RTH / post-market bars are all "
        "distinguishable. 09:30 ET (RTH open) = 13:30 UTC in summer. The date= "
        "partition is the ET trading session date from the source filename; in "
        "winter a late post-market bar can therefore carry a UTC ts on the "
        "following calendar day. ts is timezone-naive and semantically UTC."
    ),
}
TS_CONVENTION["5min"] = TS_CONVENTION["1min"].replace("minute the bar", "5-minute bucket the bar")
TS_CONVENTION["15min"] = TS_CONVENTION["1min"].replace("minute the bar", "15-minute bucket the bar")


@dataclass(frozen=True)
class MinioConfig:
    """Resolved MinIO connection settings."""

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str

    @property
    def host(self) -> str:
        """endpoint without the scheme, for DuckDB's ``s3_endpoint`` setting."""
        return self.endpoint_url.split("://", 1)[-1]

    @property
    def use_ssl(self) -> bool:
        return self.endpoint_url.lower().startswith("https://")


def _first_env(*names: str) -> str | None:
    """Return the first set, non-empty environment variable among ``names``."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def resolve_minio_config() -> MinioConfig:
    """Resolve MinIO settings from the environment, accepting both name sets.

    Returns
    -------
    MinioConfig
        Fully populated connection settings.

    Raises
    ------
    RuntimeError
        If any required setting is missing.
    """
    endpoint = _first_env("MINIO_ENDPOINT_URL", "MINIO_URL")
    access = _first_env("MINIO_ACCESS_KEY_ID", "MINIO_USERNAME")
    secret = _first_env("MINIO_SECRET_ACCESS_KEY", "MINIO_PASSWORD")
    bucket = _first_env("MINIO_BUCKET")

    missing = [
        label
        for label, value in (
            ("MINIO_ENDPOINT_URL/MINIO_URL", endpoint),
            ("MINIO_ACCESS_KEY_ID/MINIO_USERNAME", access),
            ("MINIO_SECRET_ACCESS_KEY/MINIO_PASSWORD", secret),
            ("MINIO_BUCKET", bucket),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing MinIO credentials in environment/.env: " + ", ".join(missing)
        )

    return MinioConfig(
        endpoint_url=endpoint,
        access_key=access,
        secret_key=secret,
        bucket=bucket,
    )


def make_s3_client(config: MinioConfig, max_pool: int = 64):
    """Build a boto3 S3 client pointed at MinIO (path-style, SigV4)."""
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            max_pool_connections=max_pool,
            retries={"max_attempts": 10, "mode": "standard"},
        ),
    )


def connect_duckdb(config: MinioConfig, threads: int = 1) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection configured to read/write MinIO.

    Each worker thread should call this for its own connection -- the httpfs
    settings are connection-global.

    Parameters
    ----------
    config
        Resolved MinIO settings.
    threads
        DuckDB thread count for this connection.

    Returns
    -------
    duckdb.DuckDBPyConnection
        A connection with httpfs loaded and S3 credentials applied.
    """
    con = duckdb.connect(database=":memory:")
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"PRAGMA threads={int(threads)}")
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute(f"SET s3_endpoint='{config.host}'")
    con.execute("SET s3_url_style='path'")
    con.execute(f"SET s3_use_ssl={'true' if config.use_ssl else 'false'}")
    con.execute(f"SET s3_access_key_id='{config.access_key}'")
    con.execute(f"SET s3_secret_access_key='{config.secret_key}'")
    # Wide multi-file scans (thousands of small partitions) can momentarily
    # exhaust MinIO's connection budget and get a connection refused. Retry with
    # backoff so a transient refusal never fails an honest query.
    con.execute("SET http_keep_alive=true")
    con.execute("SET http_retries=10")
    con.execute("SET http_retry_wait_ms=200")
    con.execute("SET http_retry_backoff=2")
    con.execute("SET http_timeout=120000")
    return con


def s3_uri(config: MinioConfig, key: str) -> str:
    """Build a ``s3://bucket/key`` URI for DuckDB."""
    return f"s3://{config.bucket}/{key.lstrip('/')}"


def partition_key(timeframe: str, day: str) -> str:
    """Object key of one date partition, e.g. ``bars/tf=1min/date=2024-06-10/part.parquet``.

    Parameters
    ----------
    timeframe
        One of :data:`TIMEFRAMES`.
    day
        ISO date string ``YYYY-MM-DD``.
    """
    return f"{BARS_PREFIX}/tf={timeframe}/date={day}/part.parquet"


def timeframe_glob(timeframe: str) -> str:
    """Glob key matching every partition of one timeframe."""
    return f"{BARS_PREFIX}/tf={timeframe}/**/*.parquet"


def meta_key(timeframe: str) -> str:
    """Object key of a timeframe's ``_meta.json``."""
    return f"{BARS_PREFIX}/tf={timeframe}/_meta.json"
