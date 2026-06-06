"""Key-level extraction. All levels are causal — at bar t, only data through t-1 (or
the running session up to t) is used. Verified by tests/test_setups.py."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import rth_mask, session_session_ny


def attach_session_id(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["session"] = session_session_ny(out)
    return out


def prior_day_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Add PDH / PDL / PDC columns. At bar t, these reference the *prior* RTH session."""
    rth = rth_mask(df)
    daily = (
        df[rth]
        .assign(session=session_session_ny(df[rth]))
        .groupby("session")
        .agg(pdh=("high", "max"), pdl=("low", "min"), pdc=("close", "last"))
    )
    # Shift by 1 session so each row sees only the prior day's levels (no peeking).
    daily = daily.shift(1)

    sess = session_session_ny(df)
    joined = df.join(daily, on=sess.values)
    return joined


def opening_range(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """Add ORH / ORL columns. ORH/ORL are NaN until the OR window closes for that
    session; from the close of the window onward they are the high/low of the first
    `minutes` of RTH. Strictly causal."""
    out = df.copy()
    rth = rth_mask(out)
    sess = session_session_ny(out)

    out["_rth"] = rth
    out["_session"] = sess

    # First RTH timestamp per session (manual loop — robust to empty groups)
    rth_only = out[rth].copy()
    rth_only["_session"] = session_session_ny(rth_only)

    orh_map: dict = {}
    orl_map: dict = {}
    or_close_map: dict = {}
    window = pd.Timedelta(minutes=minutes)
    if not rth_only.empty:
        for session in rth_only["_session"].unique():
            sess_rows = rth_only[rth_only["_session"] == session]
            if sess_rows.empty:
                continue
            t0 = sess_rows.index[0]
            t1 = t0 + window
            win = sess_rows.loc[(sess_rows.index >= t0) & (sess_rows.index < t1)]
            if win.empty:
                continue
            orh_map[session] = win["high"].max()
            orl_map[session] = win["low"].min()
            or_close_map[session] = t1

    out["orh"] = np.nan
    out["orl"] = np.nan
    for session in orh_map:
        mask = (out["_session"] == session) & (out.index >= or_close_map[session])
        out.loc[mask, "orh"] = orh_map[session]
        out.loc[mask, "orl"] = orl_map[session]

    return out.drop(columns=["_rth", "_session"])


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """RTH session VWAP, reset daily. Cumulative — causal."""
    rth = rth_mask(df)
    sess = session_session_ny(df)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    pv_cum = pv.where(rth, 0).groupby(sess.values).cumsum()
    vol_cum = df["volume"].where(rth, 0).groupby(sess.values).cumsum()
    vwap = (pv_cum / vol_cum).where(rth)
    vwap.name = "vwap"
    return vwap


def running_session_hilo(df: pd.DataFrame) -> pd.DataFrame:
    """Running session high / low (HOD / LOD) — causal expanding max/min within session."""
    sess = session_session_ny(df)
    rth = rth_mask(df)
    hi = df["high"].where(rth)
    lo = df["low"].where(rth)
    hod = hi.groupby(sess.values).cummax()
    lod = lo.groupby(sess.values).cummin()
    return pd.DataFrame({"hod": hod, "lod": lod}, index=df.index)


def attach_all_levels(df: pd.DataFrame, or_minutes: int = 15) -> pd.DataFrame:
    """One-shot: attach PDH/PDL/PDC, ORH/ORL, VWAP, HOD/LOD. Returns enriched copy."""
    out = prior_day_levels(df)
    out = opening_range(out, minutes=or_minutes)
    out["vwap"] = session_vwap(df)
    hodlo = running_session_hilo(df)
    out["hod"] = hodlo["hod"]
    out["lod"] = hodlo["lod"]
    return out
