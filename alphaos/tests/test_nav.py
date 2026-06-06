"""Unit tests for the AlphaOS DB services (config / allocation / NAV ledger).

Each test runs against a fresh in-memory SQLite database so the suite stays
fast and deterministic and never touches the real Postgres backend. The Session
is passed straight to the service functions, exactly as in production.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import Base
from alphaos.db.config import get_config, update_config, target_leverage
from alphaos.db.allocation import (
    seed_default_sleeves,
    list_sleeves,
    upsert_holding,
    allocation,
    total_gross_value,
)
from alphaos.db.nav import add_snapshot, latest_snapshot, list_snapshots


def _dec(value) -> Decimal:
    """Decimal-from-anything, so float column round-trips compare cleanly."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


@pytest.fixture()
def session():
    """A fresh, isolated in-memory DB + Session for every test."""
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
# config
# --------------------------------------------------------------------------- #

def test_get_config_creates_singleton_with_defaults(session):
    cfg = get_config(session)

    # Singleton: row id is always 1, and a second call returns the same row.
    assert cfg.id == 1
    assert get_config(session) is cfg

    assert _dec(cfg.leverage_target) == Decimal("1.30")
    assert _dec(cfg.leverage_floor) == Decimal("1.00")
    assert _dec(cfg.delever_half_dd) == Decimal("-0.35")
    assert _dec(cfg.delever_full_dd) == Decimal("-0.45")
    assert _dec(cfg.forced_sale_dd) == Decimal("-0.57")
    assert _dec(cfg.glide_low_assets) == Decimal("2500000")
    assert _dec(cfg.glide_high_assets) == Decimal("10000000")


def test_target_leverage_glide_path(session):
    cfg = get_config(session)
    lo = _dec(cfg.glide_low_assets)      # 2_500_000
    hi = _dec(cfg.glide_high_assets)     # 10_000_000

    # Below the glide floor -> full leverage_target.
    assert target_leverage(cfg, lo - Decimal("1")) == Decimal("1.30")
    assert target_leverage(cfg, Decimal("0")) == Decimal("1.30")
    # At/above the glide ceiling -> leverage_floor.
    assert target_leverage(cfg, hi) == Decimal("1.00")
    assert target_leverage(cfg, hi + Decimal("5000000")) == Decimal("1.00")
    # Midpoint -> linear interpolation halfway between 1.30 and 1.00 == 1.15.
    midpoint = (lo + hi) / 2
    assert target_leverage(cfg, midpoint) == Decimal("1.15")


def test_update_config_only_touches_editable_fields(session):
    cfg = update_config(session, leverage_target=Decimal("1.25"), id=999)
    assert _dec(cfg.leverage_target) == Decimal("1.25")
    assert cfg.id == 1  # 'id' is not in the editable allow-list.


# --------------------------------------------------------------------------- #
# allocation
# --------------------------------------------------------------------------- #

def test_seed_default_sleeves_and_target_weights(session):
    n = seed_default_sleeves(session)
    assert n == 5

    sleeves = list_sleeves(session)
    assert len(sleeves) == 5
    weight_sum = sum((_dec(s.target_weight) for s in sleeves), Decimal("0"))
    assert weight_sum == Decimal("1.00")

    # Idempotent: re-seeding adds nothing new.
    assert seed_default_sleeves(session) == 0
    assert len(list_sleeves(session)) == 5


def test_allocation_weights_drift_and_rebalance(session):
    seed_default_sleeves(session)

    # Put holdings across sleeves with deliberately off-target values.
    # Holdings are valued qty * price * FX; with qty=1, SEK last_price, FX=1 the
    # market value equals last_price.
    def _hold(code, symbol, value):
        upsert_holding(session, sleeve_code=code, symbol=symbol,
                       currency="SEK", quantity=1, last_price=value)

    _hold("CNDX", "CNDX", 300_000)
    _hold("VVSM", "VVSM", 100_000)
    _hold("RAW", "AAPL", 400_000)
    _hold("CA", "IDTL", 100_000)
    _hold("LOWVOL", "BAB", 100_000)

    assert total_gross_value(session) == Decimal("1000000")

    alloc = allocation(session)
    assert alloc["total_gross_value"] == pytest.approx(1_000_000.0)
    assert alloc["target_weight_sum"] == pytest.approx(1.0)

    by_code = {row["code"]: row for row in alloc["sleeves"]}

    # CNDX: current 300k/1M = 0.30 vs target 0.24 -> drift +0.06.
    cndx = by_code["CNDX"]
    assert cndx["current_weight"] == pytest.approx(0.30)
    assert cndx["target_weight"] == pytest.approx(0.24)
    assert cndx["drift"] == pytest.approx(0.06)
    # rebalance_delta = target_value - current_value = 240k - 300k = -60k.
    assert cndx["rebalance_delta"] == pytest.approx(-60_000.0)

    # With target weights summing to 1, all rebalance deltas net out to ~0.
    delta_sum = sum(row["rebalance_delta"] for row in alloc["sleeves"])
    assert delta_sum == pytest.approx(0.0, abs=1e-6)

    # Per-sleeve current weights sum to 1 (everything is assigned).
    weight_sum = sum(row["current_weight"] for row in alloc["sleeves"])
    assert weight_sum == pytest.approx(1.0)
    assert alloc["unassigned"]["current_value"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# NAV ledger
# --------------------------------------------------------------------------- #

def test_first_snapshot_baselines_index(session):
    snap = add_snapshot(session, as_of="2026-01-01", gross_asset_value=1_000_000, loan_balance=0)

    assert _dec(snap.nav_index) == Decimal("1")
    assert snap.twr_period is None
    assert _dec(snap.drawdown) == Decimal("0")
    assert _dec(snap.peak_nav_index) == Decimal("1")
    assert _dec(snap.equity) == Decimal("1000000")
    assert snap.delever_status.value == "normal"
    assert latest_snapshot(session) is snap


def test_second_snapshot_twr_excludes_contribution(session):
    add_snapshot(session, as_of="2026-01-01", gross_asset_value=1_000_000, loan_balance=0)
    snap = add_snapshot(
        session,
        as_of="2026-02-01",
        gross_asset_value=1_300_000,
        loan_balance=100_000,
        net_contribution=100_000,
    )

    # equity_end = 1_300_000 - 100_000 = 1_200_000; equity_begin = 1_000_000.
    # twr = (1_200_000 - 1_000_000 - 100_000) / 1_000_000 = 0.10 (contrib stripped).
    assert _dec(snap.equity) == Decimal("1200000")
    assert _dec(snap.twr_period) == Decimal("0.10")
    # nav_index links: 1.0 * (1 + 0.10) = 1.10.
    assert _dec(snap.nav_index) == Decimal("1.10")
    # effective_leverage = gross/equity; belaningsgrad = loan/gross.
    assert _dec(snap.effective_leverage) == pytest.approx(
        Decimal("1300000") / Decimal("1200000")
    )
    assert _dec(snap.belaningsgrad) == pytest.approx(
        Decimal("100000") / Decimal("1300000")
    )
    # New peak reached, so no drawdown.
    assert _dec(snap.drawdown) == Decimal("0")
    assert _dec(snap.peak_nav_index) == Decimal("1.10")

    assert len(list_snapshots(session)) == 2


def test_delever_status_half(session):
    add_snapshot(session, as_of="2026-01-01", gross_asset_value=1_000_000, loan_balance=0)
    # equity falls to 640k -> twr -0.36 -> nav_index 0.64 -> drawdown -0.36 <= -0.35.
    snap = add_snapshot(session, as_of="2026-02-01", gross_asset_value=640_000, loan_balance=0)

    assert _dec(snap.drawdown) == Decimal("-0.36")
    assert snap.delever_status.value == "half"


def test_delever_status_full(session):
    add_snapshot(session, as_of="2026-01-01", gross_asset_value=1_000_000, loan_balance=0)
    # equity falls to 540k -> twr -0.46 -> nav_index 0.54 -> drawdown -0.46 <= -0.45.
    snap = add_snapshot(session, as_of="2026-02-01", gross_asset_value=540_000, loan_balance=0)

    assert _dec(snap.drawdown) == Decimal("-0.46")
    assert snap.delever_status.value == "full"


def test_add_snapshot_nonpositive_equity_raises(session):
    with pytest.raises(ValueError):
        add_snapshot(session, as_of="2026-01-01", gross_asset_value=100_000, loan_balance=100_000)
