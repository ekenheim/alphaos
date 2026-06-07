"""Latest daily close from MinIO `stocks-us` (to value US-stock holdings).

The bucket is a hive layout: bars/tf=1day/date=YYYY-MM-DD/part.parquet with
columns including `ticker` and `close`. We read the single most-recent daily
partition and pull closes for the requested tickers (yesterday's close in
practice). Only US stocks present in the bucket get priced; everything else
(EUR UCITS ETFs, etc.) is left for manual pricing.
"""

from __future__ import annotations

import io
import os
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

_BUCKET = os.getenv("MINIO_BUCKET") or os.getenv("S3_BUCKET") or "stocks-us"
_URL = os.getenv("MINIO_ENDPOINT_URL") or os.getenv("MINIO_URL") or "http://s3-lan.ekenhome.se:9000"


def have_credentials() -> bool:
    return bool(os.getenv("MINIO_SECRET_ACCESS_KEY") or os.getenv("MINIO_PASSWORD"))


def bucket() -> str:
    return _BUCKET


def endpoint() -> str:
    return _URL


def _client():
    import boto3
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=_URL,
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY_ID") or os.getenv("MINIO_USERNAME"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_ACCESS_KEY") or os.getenv("MINIO_PASSWORD"),
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
        region_name="us-east-1",
    )


def _latest_daily_partition(s3, tf: str = "1day") -> tuple[date, str] | None:
    prefix = f"bars/tf={tf}/"
    paginator = s3.get_paginator("list_objects_v2")
    best: tuple[date, str] | None = None
    for page in paginator.paginate(Bucket=_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet"):
                continue
            try:
                d = date.fromisoformat(key.split("date=", 1)[1].split("/", 1)[0])
            except (IndexError, ValueError):
                continue
            if best is None or d > best[0]:
                best = (d, key)
    return best


def latest_closes(symbols: list[str], tf: str = "1day") -> dict[str, tuple[Decimal, date]]:
    """Return {TICKER: (close, date)} for symbols present in the latest daily partition."""
    wanted = {s.strip().upper() for s in symbols if s and s.strip()}
    if not wanted or not have_credentials():
        return {}
    import pandas as pd

    s3 = _client()
    latest = _latest_daily_partition(s3, tf)
    if latest is None:
        return {}
    d, key = latest
    obj = s3.get_object(Bucket=_BUCKET, Key=key)
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
    if "ticker" not in df.columns or "close" not in df.columns:
        return {}
    tick = df["ticker"].astype(str).str.upper()
    sub = df[tick.isin(wanted)]
    out: dict[str, tuple[Decimal, date]] = {}
    for _, r in sub.iterrows():
        out[str(r["ticker"]).upper()] = (Decimal(str(r["close"])), d)
    return out


def closes_in_range(
    symbols: list[str], start: date, end: date, tf: str = "1day"
) -> dict[date, dict[str, Decimal]]:
    """Return {date: {TICKER: close}} for every daily partition in [start, end].

    Used to reconstruct a historical NAV series — values holdings at each day's
    close rather than only the latest one. Empty if MinIO is unreachable/unconfigured.
    """
    wanted = {s.strip().upper() for s in symbols if s and s.strip()}
    if not wanted or not have_credentials():
        return {}
    import pandas as pd

    s3 = _client()
    prefix = f"bars/tf={tf}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys_by_date: dict[date, list[str]] = {}
    for page in paginator.paginate(Bucket=_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet"):
                continue
            try:
                d = date.fromisoformat(key.split("date=", 1)[1].split("/", 1)[0])
            except (IndexError, ValueError):
                continue
            if start <= d <= end:
                keys_by_date.setdefault(d, []).append(key)

    out: dict[date, dict[str, Decimal]] = {}
    for d, keys in keys_by_date.items():
        day: dict[str, Decimal] = {}
        for key in keys:
            obj = s3.get_object(Bucket=_BUCKET, Key=key)
            df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
            if "ticker" not in df.columns or "close" not in df.columns:
                continue
            tick = df["ticker"].astype(str).str.upper()
            sub = df[tick.isin(wanted)]
            for _, r in sub.iterrows():
                day[str(r["ticker"]).upper()] = Decimal(str(r["close"]))
        if day:
            out[d] = day
    return out


def refresh_prices(session: Session, tf: str = "1day") -> dict[str, Any]:
    """Update last_price/last_price_date for holdings whose symbol is in stocks-us.

    Returns a status dict; never raises on MinIO failure.
    """
    from .allocation import list_holdings
    from .models import PriceSource

    if not have_credentials():
        return {"ok": False, "error": "MinIO credentials not configured", "updated": 0, "skipped": []}

    holdings = list_holdings(session)
    symbols = [h.symbol for h in holdings if h.symbol]
    try:
        closes = latest_closes(symbols, tf)
    except Exception as e:  # network / bucket error -> graceful
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "updated": 0, "skipped": symbols}

    updated = 0
    skipped: list[str] = []
    as_of: date | None = None
    for h in holdings:
        key = (h.symbol or "").upper()
        if key in closes:
            price, d = closes[key]
            h.last_price = price
            h.last_price_date = d
            h.price_source = PriceSource.minio
            as_of = d
            updated += 1
        elif h.symbol:
            skipped.append(h.symbol)
    session.flush()
    return {
        "ok": True,
        "updated": updated,
        "skipped": skipped,
        "as_of": as_of.isoformat() if as_of else None,
        "bucket": _BUCKET,
    }
