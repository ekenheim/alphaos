"""MinIO/S3 loader for AlphaOS — reads a Polygon-style hive partition layout.

Expected bucket layout:
    bars/tf={1min,5min,15min,1day}/date=YYYY-MM-DD/part.parquet
    meta/universe.parquet         -- ticker, first_date, last_date, ...
    reference/splits.parquet      -- ticker, execution_date, split_from, split_to

This loader:
  - Pulls only the date partitions in the requested window
  - Filters by ticker inside each partition
  - Back-adjusts for splits
  - Caches the result as a per-ticker parquet so subsequent loads are instant

Credentials are loaded from these .env locations, in order:
  1. ALPHAOS_ENV_FILE  (env var pointing at an .env path)
  2. ./.env (current working dir)
  3. <package>/.env  (alongside alphaos/__init__.py)
  4. ../trading-research/.env  (legacy, when used inside the ruflo monorepo)

Expected keys (any of the aliases):
    MINIO_URL / MINIO_ENDPOINT_URL
    MINIO_USERNAME / MINIO_ACCESS_KEY_ID
    MINIO_PASSWORD / MINIO_SECRET_ACCESS_KEY
    MINIO_BUCKET / S3_BUCKET                (default 'stocks-us')
"""

from __future__ import annotations

import io
import os
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Iterable

import pandas as pd


def _load_env() -> None:
    """Walk the .env candidate list and load the first that exists."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = Path(__file__).resolve().parent
    candidates = [
        os.environ.get("ALPHAOS_ENV_FILE"),
        Path.cwd() / ".env",
        here / ".env",
        here.parent / "trading-research" / ".env",  # legacy
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if p.exists():
            load_dotenv(p, override=False)
            return


_load_env()

def _ensure_cache_dir(preferred: Path) -> Path:
    """Create `preferred`, or fall back to a temp dir if it isn't writable.

    Importing from a read-only location (e.g. site-packages in a container) must
    not crash; fall back to the system temp dir for the cache in that case.
    """
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        import tempfile

        alt = Path(tempfile.gettempdir()) / "alphaos_cache" / preferred.name
        try:
            alt.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return alt


CACHE_ROOT = _ensure_cache_dir(Path(__file__).resolve().parent / "data_cache" / "minio")

_BUCKET = os.getenv("MINIO_BUCKET") or os.getenv("S3_BUCKET") or "stocks-us"
_URL = os.getenv("MINIO_ENDPOINT_URL") or os.getenv("MINIO_URL") or "http://s3-lan.ekenhome.se:9000"

VALID_TFS = ("1min", "5min", "15min", "1day")


def _make_client():
    """Boto3 S3 client, same tuning as MinioIntradayFetcher."""
    import boto3
    from botocore.client import Config
    return boto3.client(
        "s3",
        endpoint_url=_URL,
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY_ID") or os.getenv("MINIO_USERNAME"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_ACCESS_KEY") or os.getenv("MINIO_PASSWORD"),
        config=Config(
            signature_version="s3v4",
            max_pool_connections=32,
            retries={"max_attempts": 3, "mode": "adaptive"},
            s3={"addressing_style": "path"},
        ),
        region_name="us-east-1",
    )


_locks: dict[str, Lock] = {}
_splits_cache: dict[str, list[tuple[pd.Timestamp, float]]] | None = None


def _get_lock(key: str) -> Lock:
    if key not in _locks:
        _locks[key] = Lock()
    return _locks[key]


def have_credentials() -> bool:
    return bool(os.getenv("MINIO_SECRET_ACCESS_KEY") or os.getenv("MINIO_PASSWORD"))


def _load_splits(s3) -> dict[str, list[tuple[pd.Timestamp, float]]]:
    global _splits_cache
    if _splits_cache is not None:
        return _splits_cache
    out: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    try:
        obj = s3.get_object(Bucket=_BUCKET, Key="reference/splits.parquet")
        df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
        for tk, g in df.groupby("ticker"):
            rows = []
            for _, r in g.iterrows():
                sf, st = float(r["split_from"]), float(r["split_to"])
                if sf > 0 and st > 0:
                    ed = pd.Timestamp(r["execution_date"], tz="UTC").normalize()
                    rows.append((ed, st / sf))
            if rows:
                out[str(tk)] = rows
    except Exception:
        # Fail-open: no splits → serve raw bars
        pass
    _splits_cache = out
    return out


def _list_partitions(s3, tf: str, start: date, end: date) -> list[tuple[date, str]]:
    """List date partitions in [start, end]. Returns sorted [(date, key)]."""
    prefix = f"bars/tf={tf}/"
    paginator = s3.get_paginator("list_objects_v2")
    parts: list[tuple[date, str]] = []
    for page in paginator.paginate(Bucket=_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet"):
                continue
            # bars/tf=5min/date=2016-06-01/part.parquet  ->  2016-06-01
            try:
                d_str = key.split("date=", 1)[1].split("/", 1)[0]
                d = date.fromisoformat(d_str)
            except (IndexError, ValueError):
                continue
            if start <= d <= end:
                parts.append((d, key))
    parts.sort()
    return parts


def _read_partition(s3, key: str, ticker: str) -> pd.DataFrame | None:
    """Read one date partition, filter to `ticker`, return raw rows or None.

    Actual bars/ schema (probed 2026-06-06): columns = ticker, ts (datetime64[us]),
    open, high, low, close, volume, transactions. No window_start.
    """
    obj = s3.get_object(Bucket=_BUCKET, Key=key)
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
    if "ticker" not in df.columns:
        return None
    df = df[df["ticker"] == ticker]
    if df.empty:
        return None
    return df


def fetch_ticker(
    ticker: str,
    tf: str = "5min",
    start: date | str | None = None,
    end: date | str | None = None,
    use_cache: bool = True,
    progress: bool = False,
) -> pd.DataFrame | None:
    """Pull a ticker's bars from MinIO across a date window, cache as per-ticker parquet.

    First call for (ticker, tf) scans every partition in [start, end] (slow — minutes).
    Subsequent calls hit the local cache (instant).

    Returns: UTC-indexed DataFrame with [open, high, low, close, volume] or None.
    """
    if tf not in VALID_TFS:
        raise ValueError(f"tf {tf!r} not in {VALID_TFS}")
    if not have_credentials():
        return None

    end = pd.Timestamp(end).date() if end else date.today()
    start = pd.Timestamp(start).date() if start else date(2016, 6, 1)

    cache_path = CACHE_ROOT / f"{ticker}_{tf}.parquet"
    if use_cache and cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
            cached.index = pd.DatetimeIndex(cached.index).tz_convert("UTC")
            cmin, cmax = cached.index.min().date(), cached.index.max().date()
            if cmin <= start and cmax >= end:
                mask = (cached.index.date >= start) & (cached.index.date <= end)
                return cached.loc[mask]
        except Exception:
            cache_path.unlink(missing_ok=True)

    with _get_lock(f"{ticker}_{tf}"):
        s3 = _make_client()
        parts = _list_partitions(s3, tf, start, end)
        if not parts:
            return None

        frames: list[pd.DataFrame] = []
        for i, (d, key) in enumerate(parts):
            try:
                sub = _read_partition(s3, key, ticker)
            except Exception:
                continue
            if sub is not None:
                frames.append(sub)
            if progress and (i + 1) % 50 == 0:
                print(f"  [{ticker} {tf}] {i+1}/{len(parts)} partitions, {len(frames)} matched", flush=True)

        if not frames:
            return None

        df = pd.concat(frames, axis=0, ignore_index=True)
        # Schema variants seen: 'ts' (datetime64[us], current) or 'window_start' (ns int)
        if "ts" in df.columns:
            idx = pd.to_datetime(df["ts"], utc=True)
        elif "window_start" in df.columns:
            idx = pd.to_datetime(df["window_start"].astype("int64"), unit="ns", utc=True)
        else:
            return None
        df.index = idx
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        keep = [c for c in ("open", "high", "low", "close", "volume", "transactions")
                if c in df.columns]
        df = df[keep].copy()

        # Split adjustment
        splits = _load_splits(s3).get(ticker, [])
        if splits:
            factor = pd.Series(1.0, index=df.index)
            for exec_ts, ratio in splits:
                factor.loc[df.index < exec_ts] *= (1.0 / ratio)
            for c in ("open", "high", "low", "close"):
                df[c] = df[c] * factor

        try:
            df.to_parquet(cache_path)
        except Exception:
            pass
        return df


# --- AlphaOSTrader instrument -> US ETF proxy mapping ---
# Honest: these are PROXIES. QQQ ≠ NQ futures (no overnight session, etc.), but
# for a research backtest they capture the underlying index dynamics.

ETF_PROXY: dict[str, str] = {
    "US100":  "QQQ",   # NASDAQ-100 ETF
    "SPY":    "SPY",
    "US500":  "SPY",
    "US30":   "DIA",   # Dow Jones ETF
    "XAUUSD": "GLD",   # Gold ETF
    "BTCUSD": "IBIT",  # iShares Bitcoin Trust (Jan 2024+); for longer history use BITO (Oct 2021+)
    "JP225":  "EWJ",   # MSCI Japan ETF
    # USDJPY: no direct equity proxy; FXY (Japanese Yen ETF) is loose at best
}


def resolve_proxy(symbol: str) -> str | None:
    """Map a AlphaOSTrader-style symbol to a MinIO-available US ETF, or None."""
    return ETF_PROXY.get(symbol, symbol if symbol.isupper() and len(symbol) <= 5 else None)


_RESAMPLE_RULE = {
    "1min": "1min", "5min": "5min", "15min": "15min",
    "30min": "30min", "1h": "1h", "1H": "1h", "1d": "1D", "1day": "1D",
}


def resample(df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """Standard OHLCV resample. Source must be a UTC-indexed bar series."""
    rule = _RESAMPLE_RULE.get(target_tf, target_tf)
    out = df.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])
    if "transactions" in df.columns:
        tx = df["transactions"].resample(rule, label="left", closed="left").sum()
        out["transactions"] = tx.reindex(out.index).fillna(0).astype("int64")
    return out


def fetch_resampled(
    symbol: str,
    target_tf: str = "1h",
    source_tf: str = "5min",
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame | None:
    """High-level helper: pull source_tf from MinIO, resample to target_tf,
    accept a AlphaOSTrader symbol and auto-proxy it."""
    proxy = resolve_proxy(symbol) or symbol
    raw = fetch_ticker(proxy, tf=source_tf, start=start, end=end)
    if raw is None:
        return None
    if target_tf == source_tf:
        return raw
    return resample(raw, target_tf)
