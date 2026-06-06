"""Unit tests for FX conversion, holding valuation, and the offline-safe
pricing/FX refresh paths.

Runs against in-memory SQLite with NO network and NO MinIO: the MinIO refresh
is exercised with credentials unset, and the FX refresh is forced offline by
monkeypatching fetch_rates to return None. Fast and deterministic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import alphaos.db.fx as fx_mod
import alphaos.db.pricing as pricing_mod
from alphaos.db.allocation import holding_valuation, upsert_holding
from alphaos.db.config import get_config, update_config
from alphaos.db.fx import fx_to_sek
from alphaos.db.models import Base, PriceSource


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine)
    sess: Session = Maker()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


# --------------------------------------------------------------------------- #
# fx_to_sek
# --------------------------------------------------------------------------- #

def test_fx_to_sek_per_currency(session):
    cfg = update_config(session, fx_usd_sek=Decimal("10"), fx_eur_sek=Decimal("11"))

    assert fx_to_sek(cfg, "SEK") == Decimal("1")
    assert fx_to_sek(cfg, None) == Decimal("1")
    assert fx_to_sek(cfg, "USD") == _dec(cfg.fx_usd_sek) == Decimal("10")
    assert fx_to_sek(cfg, "EUR") == _dec(cfg.fx_eur_sek) == Decimal("11")
    # Unknown currency is treated as already-SEK.
    assert fx_to_sek(cfg, "GBP") == Decimal("1")


# --------------------------------------------------------------------------- #
# holding_valuation
# --------------------------------------------------------------------------- #

def test_valuation_with_last_price(session):
    cfg = update_config(session, fx_usd_sek=Decimal("10"))
    h = upsert_holding(
        session,
        symbol="AAPL",
        isin="US0378331005",
        currency="USD",
        quantity=Decimal("10"),
        avg_price=Decimal("50"),
        cost_basis_sek=Decimal("5000"),
        last_price=Decimal("60"),
        price_source=PriceSource.minio,
    )

    v = holding_valuation(cfg, h)
    # market_value = qty * last_price * fx = 10 * 60 * 10 = 6000.
    assert v["market_value"] == Decimal("6000")
    assert v["price"] == Decimal("60")
    assert v["fx"] == Decimal("10")
    assert v["price_source"] == "minio"
    assert v["cost_basis"] == Decimal("5000")
    assert v["unrealized_pnl"] == Decimal("6000") - Decimal("5000") == Decimal("1000")


def test_valuation_without_last_price_uses_cost(session):
    cfg = update_config(session, fx_usd_sek=Decimal("10"))
    h = upsert_holding(
        session,
        symbol="AAPL",
        isin="US0378331005",
        currency="USD",
        quantity=Decimal("10"),
        avg_price=Decimal("50"),
        # no last_price, no cost_basis_sek -> valued at cost (avg_price).
    )

    v = holding_valuation(cfg, h)
    # Priced at cost: market_value = qty * avg_price * fx = 10 * 50 * 10 = 5000.
    assert v["price"] == Decimal("50")
    assert v["market_value"] == Decimal("5000")
    assert v["price_source"] == "cost"
    # cost_basis falls back to qty * avg_price * fx -> PnL is exactly zero.
    assert v["cost_basis"] == Decimal("5000")
    assert v["unrealized_pnl"] == v["market_value"] - v["cost_basis"] == Decimal("0")


# --------------------------------------------------------------------------- #
# pricing.refresh_prices — no MinIO creds
# --------------------------------------------------------------------------- #

def test_refresh_prices_without_credentials(session, monkeypatch):
    # Ensure no MinIO credentials are visible to have_credentials().
    monkeypatch.delenv("MINIO_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("MINIO_PASSWORD", raising=False)
    assert pricing_mod.have_credentials() is False

    upsert_holding(session, symbol="AAPL", currency="USD", quantity=Decimal("10"))

    res = pricing_mod.refresh_prices(session)  # must not raise
    assert res["ok"] is False
    assert res["updated"] == 0


# --------------------------------------------------------------------------- #
# fx.refresh_fx — offline path keeps cached rates
# --------------------------------------------------------------------------- #

def test_refresh_fx_offline_keeps_cached_rates(session, monkeypatch):
    # Seed known cached rates.
    update_config(session, fx_usd_sek=Decimal("9.5"), fx_eur_sek=Decimal("10.5"))

    # Force the offline path: no rates fetched.
    monkeypatch.setattr(fx_mod, "fetch_rates", lambda *a, **k: None)

    res = fx_mod.refresh_fx(session)  # must not raise
    assert res["ok"] is False
    assert res["usd_sek"] == pytest.approx(9.5)
    assert res["eur_sek"] == pytest.approx(10.5)

    # Cached rates on the config are preserved.
    cfg = get_config(session)
    assert _dec(cfg.fx_usd_sek) == Decimal("9.5")
    assert _dec(cfg.fx_eur_sek) == Decimal("10.5")
