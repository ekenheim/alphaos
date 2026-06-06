"""OHLCV loader with parquet-first priority + yfinance fallback.

Loader priority:
  1. $ALPHAOS_PARQUET_DIR/{SYMBOL}_{INTERVAL}.parquet  (drop-in for MinIO / your own store)
  2. alphaos/data_cache/parquet/{SYMBOL}_{INTERVAL}.parquet (project-local override)
  3. alphaos/data_cache/{SYMBOL}__{INTERVAL}__{start}__{end}.parquet (yfinance cache)
  4. yfinance download (slowest; caches into 3)

Parquet schema for (1) and (2): DatetimeIndex (UTC) named `ts_utc`, columns
[`open`, `high`, `low`, `close`, `volume`], sorted ascending, no NaNs in OHLC.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd


def _ensure_cache_dir(preferred: Path) -> Path:
    """Create `preferred`, or fall back to a temp dir if it isn't writable.

    The package may be installed in a read-only location (e.g. site-packages in a
    container). Creating the cache dir at import time must never crash; if the
    preferred path can't be made, cache under the system temp dir instead.
    """
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        alt = Path(tempfile.gettempdir()) / "alphaos_cache" / preferred.name
        try:
            alt.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return alt


CACHE_DIR = _ensure_cache_dir(Path(__file__).resolve().parent / "data_cache")
LOCAL_PARQUET_DIR = _ensure_cache_dir(CACHE_DIR / "parquet")

ENV_PARQUET_DIR = os.environ.get("ALPHAOS_PARQUET_DIR")

Interval = Literal["1m", "5m", "15m", "30m", "1h", "1d"]

# yfinance intraday lookback limits (so we don't ask for windows it can't deliver).
_INTRADAY_MAX_DAYS = {"1m": 7, "5m": 60, "15m": 60, "30m": 60, "1h": 730}


@dataclass(frozen=True)
class SymbolSpec:
    """Logical symbol -> yfinance ticker. Lets us alias ES/NQ to continuous futures."""

    name: str
    yf_ticker: str
    asset_class: str  # equity, future, crypto, fx


SYMBOLS: dict[str, SymbolSpec] = {
    "SPY":  SymbolSpec("SPY",  "SPY",     "equity"),
    "QQQ":  SymbolSpec("QQQ",  "QQQ",     "equity"),
    "IWM":  SymbolSpec("IWM",  "IWM",     "equity"),
    "NVDA": SymbolSpec("NVDA", "NVDA",    "equity"),
    "TSLA": SymbolSpec("TSLA", "TSLA",    "equity"),
    "MU":   SymbolSpec("MU",   "MU",      "equity"),
    "ES":   SymbolSpec("ES",   "ES=F",    "future"),
    "NQ":   SymbolSpec("NQ",   "NQ=F",    "future"),
    "CL":   SymbolSpec("CL",   "CL=F",    "future"),
    "GC":   SymbolSpec("GC",   "GC=F",    "future"),
    "BTC":  SymbolSpec("BTC",  "BTC-USD", "crypto"),
    "ETH":  SymbolSpec("ETH",  "ETH-USD", "crypto"),
    "EURUSD": SymbolSpec("EURUSD", "EURUSD=X", "fx"),
    "GBPUSD": SymbolSpec("GBPUSD", "GBPUSD=X", "fx"),
    # Prop-firm-style aliases — AlphaOSTrader instruments
    "US100":  SymbolSpec("US100",  "NQ=F",      "future"),   # NASDAQ-100 cash proxy
    "US30":   SymbolSpec("US30",   "YM=F",      "future"),   # Dow Jones cash proxy
    "XAUUSD": SymbolSpec("XAUUSD", "GC=F",      "future"),   # Gold
    "USDJPY": SymbolSpec("USDJPY", "USDJPY=X",  "fx"),
    "JP225":  SymbolSpec("JP225",  "^N225",     "future"),   # Nikkei 225
    "BTCUSD": SymbolSpec("BTCUSD", "BTC-USD",   "crypto"),
}


def _cache_path(symbol: str, interval: str, start: str, end: str) -> Path:
    return CACHE_DIR / f"{symbol}__{interval}__{start}__{end}.parquet"


def _try_load_user_parquet(symbol: str, interval: str) -> pd.DataFrame | None:
    """Honor the parquet-first contract. Returns None if no file found."""
    candidates: list[Path] = []
    if ENV_PARQUET_DIR:
        candidates.append(Path(ENV_PARQUET_DIR) / f"{symbol}_{interval}.parquet")
    candidates.append(LOCAL_PARQUET_DIR / f"{symbol}_{interval}.parquet")

    for path in candidates:
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        # Normalize: accept either DatetimeIndex named ts_utc or a column ts_utc / timestamp / date
        if not isinstance(df.index, pd.DatetimeIndex):
            for col in ("ts_utc", "timestamp", "date", "datetime"):
                if col in df.columns:
                    df = df.set_index(col)
                    break
        df.index = pd.DatetimeIndex(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        df.index.name = "ts_utc"
        df.columns = [c.lower() for c in df.columns]
        keep = ["open", "high", "low", "close", "volume"]
        missing = [c for c in keep if c not in df.columns]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        df = df[keep].dropna(subset=["open", "high", "low", "close"]).sort_index()
        return df
    return None


def fetch_ohlcv(
    symbol: str,
    interval: Interval = "5m",
    start: str | None = None,
    end: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Return OHLCV with columns [open, high, low, close, volume], DatetimeIndex (UTC).

    Causal-safe: callers can pass any window without smuggling future data — the
    returned frame is sorted ascending by timestamp and contains no NaNs in OHLC.
    """
    spec = SYMBOLS.get(symbol)
    if spec is None:
        # Unknown alias — pass through as raw yfinance ticker
        spec = SymbolSpec(symbol, symbol, "unknown")

    # Parquet-first: user-provided long-history file beats yfinance
    user_df = _try_load_user_parquet(spec.name, interval)
    if user_df is not None:
        if start and end:
            return user_df.loc[(user_df.index >= start) & (user_df.index <= end)]
        return user_df

    # MinIO loader (US-equity ETF proxies for AlphaOSTrader symbols).
    # Off by default — opt in via ALPHAOS_USE_MINIO=1.
    if os.environ.get("ALPHAOS_USE_MINIO") == "1":
        try:
            from . import minio as zmin
            if zmin.have_credentials():
                minio_df = zmin.fetch_resampled(
                    spec.name, target_tf=interval, source_tf="5min",
                    start=start, end=end,
                )
                if minio_df is not None and not minio_df.empty:
                    return minio_df
        except Exception:
            pass  # fall through to yfinance

    end = end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    if start is None:
        if interval == "1d":
            start = (pd.Timestamp.utcnow() - pd.Timedelta(days=365 * 5)).strftime("%Y-%m-%d")
        else:
            cap = _INTRADAY_MAX_DAYS.get(interval, 60)
            start = (pd.Timestamp.utcnow() - pd.Timedelta(days=cap - 1)).strftime("%Y-%m-%d")

    cache = _cache_path(spec.name, interval, start, end)
    if use_cache and cache.exists():
        return pd.read_parquet(cache)

    import yfinance as yf  # lazy import — keeps test imports cheap
    raw = yf.download(
        spec.yf_ticker,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if raw.empty:
        raise ValueError(f"No data for {symbol} ({spec.yf_ticker}) {interval} {start}..{end}")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    raw = raw.dropna(subset=["open", "high", "low", "close"]).sort_index()

    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    else:
        raw.index = raw.index.tz_convert("UTC")
    raw.index.name = "ts_utc"

    raw.to_parquet(cache)
    return raw


def session_session_ny(df: pd.DataFrame) -> pd.Series:
    """Return a per-row session date (US/Eastern). Sessions span the RTH day."""
    ny = df.index.tz_convert("America/New_York")
    return pd.Series(ny.normalize().date, index=df.index, name="session")


def rth_mask(df: pd.DataFrame) -> pd.Series:
    """RTH = 09:30-16:00 America/New_York. Useful for equity ORB / VWAP."""
    ny = df.index.tz_convert("America/New_York")
    t = ny.time
    from datetime import time as _t
    return pd.Series((t >= _t(9, 30)) & (t < _t(16, 0)), index=df.index)
