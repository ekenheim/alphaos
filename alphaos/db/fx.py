"""Daily FX rates to SEK, from the Riksbank (primary) or ECB (fallback).

Both sources are free and need no API key. Fetched rates are cached on
PortfolioConfig so valuation keeps working offline (cluster without egress);
the operator can also set the rates by hand on the Settings page.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.request
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from .config import get_config

# Riksbank Swea: series return SEK per 1 unit of the foreign currency.
_RIKSBANK = "https://api.riksbank.se/swea/v1/Observations/Latest/{series}"
_RIKSBANK_SERIES = {"USD": "SEKUSDPMI", "EUR": "SEKEURPMI"}
_ECB = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "alphaos/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted hosts)
        return resp.read()


def fetch_from_riksbank(timeout: float = 8.0) -> dict | None:
    rates: dict[str, Decimal] = {}
    when: str | None = None
    for ccy, series in _RIKSBANK_SERIES.items():
        try:
            data = json.loads(_http_get(_RIKSBANK.format(series=series), timeout))
            rates[ccy] = Decimal(str(data["value"]))
            when = data.get("date") or when
        except Exception:
            return None
    return {"rates": rates, "date": when, "source": "riksbank"} if rates else None


def fetch_from_ecb(timeout: float = 8.0) -> dict | None:
    """ECB publishes EUR-based rates; SEK/USD per EUR -> derive USD/SEK and EUR/SEK."""
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(_http_get(_ECB, timeout))
        when: str | None = None
        usd_per_eur: Decimal | None = None
        sek_per_eur: Decimal | None = None
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag != "Cube":
                continue
            if el.get("time"):
                when = el.get("time")
            cur, rate = el.get("currency"), el.get("rate")
            if cur == "USD" and rate:
                usd_per_eur = Decimal(rate)
            elif cur == "SEK" and rate:
                sek_per_eur = Decimal(rate)
        if sek_per_eur is None or usd_per_eur is None or usd_per_eur == 0:
            return None
        return {
            "rates": {"USD": sek_per_eur / usd_per_eur, "EUR": sek_per_eur},
            "date": when,
            "source": "ecb",
        }
    except Exception:
        return None


def fetch_rates(timeout: float = 8.0) -> dict | None:
    """Latest USD/SEK + EUR/SEK from Riksbank, falling back to ECB. None if both fail."""
    return fetch_from_riksbank(timeout) or fetch_from_ecb(timeout)


def refresh_fx(session: Session, timeout: float = 8.0) -> dict[str, Any]:
    """Fetch + persist the latest rates onto config. Never raises on network failure."""
    cfg = get_config(session)
    res = fetch_rates(timeout)
    if not res:
        return {
            "ok": False,
            "error": "FX fetch failed (no network / sources unreachable); kept cached rates",
            "usd_sek": float(cfg.fx_usd_sek),
            "eur_sek": float(cfg.fx_eur_sek),
            "as_of": cfg.fx_as_of.isoformat() if cfg.fx_as_of else None,
            "source": cfg.fx_source,
        }
    rates = res["rates"]
    if "USD" in rates:
        cfg.fx_usd_sek = rates["USD"]
    if "EUR" in rates:
        cfg.fx_eur_sek = rates["EUR"]
    if res.get("date"):
        try:
            cfg.fx_as_of = dt.date.fromisoformat(res["date"])
        except Exception:
            pass
    cfg.fx_source = res.get("source")
    session.flush()
    return {
        "ok": True,
        "usd_sek": float(cfg.fx_usd_sek),
        "eur_sek": float(cfg.fx_eur_sek),
        "as_of": cfg.fx_as_of.isoformat() if cfg.fx_as_of else None,
        "source": cfg.fx_source,
    }


def fx_to_sek(cfg, currency: str | None) -> Decimal:
    """Conversion factor from `currency` into SEK using the cached config rates."""
    c = (currency or "SEK").upper()
    if c == "SEK":
        return Decimal("1")
    if c == "USD":
        return Decimal(str(cfg.fx_usd_sek))
    if c == "EUR":
        return Decimal(str(cfg.fx_eur_sek))
    return Decimal("1")  # unknown currency -> treat as already SEK
