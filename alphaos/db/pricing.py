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


# --- yfinance / Yahoo fallback (for listings not in the MinIO stocks-us bucket) ---

_YH_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=6&newsCount=0"


def have_yfinance() -> bool:
    try:
        import yfinance  # noqa: F401
        return True
    except Exception:
        return False


def _yahoo_symbol_for_isin(isin: str, timeout: float = 8.0) -> str | None:
    """Resolve an ISIN to a Yahoo ticker via the public search endpoint.

    EUR/UCITS ETFs are usually stored under an exchange-less symbol Yahoo doesn't
    know; the ISIN search returns the exchange-suffixed symbol (e.g. CNDX -> CNDX.L)
    the quote lookup needs.
    """
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            _YH_SEARCH.format(q=isin.strip()), headers={"User-Agent": "alphaos/0.4"}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=timeout).read())  # noqa: S310
    except Exception:
        return None
    for q in data.get("quotes") or []:
        sym = q.get("symbol")
        if sym:
            return sym
    return None


def _yfinance_quote(symbol: str) -> tuple[Decimal, str | None] | None:
    """(last_price, currency) for a Yahoo symbol, or None on any failure."""
    try:
        import yfinance as yf

        fi = yf.Ticker(symbol).fast_info
        price = fi.last_price
        if price is None or float(price) <= 0:
            return None
        ccy = fi.currency
        return Decimal(str(price)), (str(ccy).upper() if ccy else None)
    except Exception:
        return None


def yahoo_fallback(symbol: str | None, isin: str | None) -> tuple[Decimal, str | None, str] | None:
    """Try yfinance by the stored symbol, then by the Yahoo symbol resolved from
    the ISIN. Returns (price, currency, used_symbol) or None. Never raises."""
    tried: list[str] = []
    if symbol:
        tried.append(symbol.strip())
    if isin:
        ys = _yahoo_symbol_for_isin(isin)
        if ys and ys not in tried:
            tried.append(ys)
    for sym in tried:
        q = _yfinance_quote(sym)
        if q:
            return q[0], q[1], sym
    return None


def refresh_prices(session: Session, tf: str = "1day") -> dict[str, Any]:
    """Update last_price for holdings: MinIO `stocks-us` first, then a yfinance/
    Yahoo fallback for anything MinIO can't price (e.g. EUR UCITS ETFs).

    Never raises on network/bucket failure. `ok` is False only when no pricing
    source is available at all.
    """
    from .allocation import list_holdings
    from .models import PriceSource

    holdings = list_holdings(session)
    have_minio = have_credentials()
    have_yf = have_yfinance()

    closes: dict[str, tuple[Decimal, date]] = {}
    minio_error: str | None = None
    if have_minio:
        try:
            closes = latest_closes([h.symbol for h in holdings if h.symbol], tf)
        except Exception as e:  # network / bucket error -> graceful
            minio_error = f"{type(e).__name__}: {e}"

    updated_minio = 0
    updated_yf = 0
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
            updated_minio += 1
            continue
        # MinIO couldn't price it -> Yahoo fallback by symbol, then by ISIN.
        if have_yf:
            q = yahoo_fallback(h.symbol, h.isin)
            if q is not None:
                price, ccy, _used = q
                h.last_price = price
                h.last_price_date = date.today()
                h.price_source = PriceSource.yfinance
                if ccy and ccy != (h.currency or "").upper():
                    h.currency = ccy  # keep currency in step with the quoted price
                updated_yf += 1
                continue
        if h.symbol or h.isin:
            skipped.append(h.symbol or h.isin)
    session.flush()
    return {
        "ok": have_minio or have_yf,
        "updated": updated_minio + updated_yf,
        "updated_minio": updated_minio,
        "updated_yfinance": updated_yf,
        "skipped": skipped,
        "as_of": as_of.isoformat() if as_of else None,
        "bucket": _BUCKET,
        "minio_error": minio_error,
        "yfinance": have_yf,
    }
