"""Portfolio config singleton (the V2-FRONTIER risk/leverage parameters)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from .models import PortfolioConfig

_EDITABLE = {
    "base_currency", "account_label",
    "leverage_target", "leverage_floor", "glide_low_assets", "glide_high_assets",
    "blended_rate", "repriced_rate", "belaningsgrad_cliff",
    "delever_half_dd", "delever_full_dd", "reentry_recovery", "forced_sale_dd",
    "delever_floor_leverage",
    "external_reserve", "planning_cagr_low", "planning_cagr_high", "notes",
    "fx_usd_sek", "fx_eur_sek",
}


def get_config(session: Session) -> PortfolioConfig:
    """Return the singleton config row, creating it with defaults if absent."""
    cfg = session.get(PortfolioConfig, 1)
    if cfg is None:
        cfg = PortfolioConfig(id=1)
        session.add(cfg)
        session.flush()
    return cfg


def update_config(session: Session, **fields: Any) -> PortfolioConfig:
    cfg = get_config(session)
    for key, value in fields.items():
        if key in _EDITABLE and value is not None:
            setattr(cfg, key, value)
    session.flush()
    return cfg


def target_leverage(cfg: PortfolioConfig, equity: Any) -> Decimal:
    """Glide-path effective leverage for a given equity size (linear interpolation).

    leverage_target below glide_low_assets, gliding to leverage_floor at
    glide_high_assets and beyond.
    """
    lo = Decimal(str(cfg.glide_low_assets))
    hi = Decimal(str(cfg.glide_high_assets))
    lev_small = Decimal(str(cfg.leverage_target))
    lev_large = Decimal(str(cfg.leverage_floor))
    e = Decimal(str(equity or 0))
    if hi <= lo or e <= lo:
        return lev_small
    if e >= hi:
        return lev_large
    frac = (e - lo) / (hi - lo)
    return lev_small + (lev_large - lev_small) * frac
