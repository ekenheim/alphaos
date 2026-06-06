"""Unit tests for the ledger + archive services.

Each test runs against a fresh in-memory SQLite database (no env / no
session_scope): we build an engine with create_engine('sqlite://'), create the
schema from Base.metadata, and hand a plain Session straight to the service
functions. Numeric columns come back as Decimal, so assertions are Decimal-aware.

Run with:  pytest alphaos/tests/test_ledger.py -q
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alphaos.db.models import (
    Action,
    Base,
    PositionStatus,
    Side,
)
from alphaos.db.ledger import (
    all_positions,
    ledger_summary,
    open_positions,
    position_detail,
    rebalance,
    record_execution,
)
from alphaos.db import archive
from alphaos.setups import SETUPS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def session():
    """A fresh, isolated in-memory SQLite Session per test."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine, future=True)
    sess: Session = Maker()
    try:
        yield sess
    finally:
        sess.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _D(x) -> Decimal:
    return Decimal(str(x))


# ---------------------------------------------------------------------------
# Ledger: open / add
# ---------------------------------------------------------------------------
def test_open_then_add_weighted_avg_and_qty(session):
    record_execution(
        session, symbol="aapl", action=Action.open, qty=100, price=10
    )
    ev = record_execution(
        session, symbol="AAPL", action="add", qty=300, price=20
    )
    session.commit()

    pos = ev.position
    # qty sums
    assert _D(pos.qty) == Decimal("400")
    # weighted avg = (100*10 + 300*20) / 400 = 7000/400 = 17.5
    assert _D(pos.avg_entry_px) == Decimal("17.5")
    assert pos.status is PositionStatus.open
    # symbol normalized to upper, both events on same position
    assert pos.symbol == "AAPL"
    assert len(pos.events) == 2
    # opening an increase realizes nothing
    assert _D(pos.realized_pnl) == Decimal("0")


# ---------------------------------------------------------------------------
# Ledger: trim (long)
# ---------------------------------------------------------------------------
def test_trim_long_realized_pnl_and_qty(session):
    record_execution(
        session, symbol="MSFT", action=Action.open, qty=100, price=50
    )
    ev = record_execution(
        session, symbol="MSFT", action=Action.trim, qty=40, price=60
    )
    session.commit()

    pos = ev.position
    # realized = (price - avg) * closed_qty = (60 - 50) * 40 = 400
    assert _D(ev.realized_pnl) == Decimal("400")
    assert _D(pos.realized_pnl) == Decimal("400")
    # qty reduced 100 -> 60, still open
    assert _D(pos.qty) == Decimal("60")
    assert pos.status is PositionStatus.open
    assert pos.closed_at is None
    # the event records the closed qty
    assert _D(ev.qty) == Decimal("40")


# ---------------------------------------------------------------------------
# Ledger: close
# ---------------------------------------------------------------------------
def test_close_long_sets_closed_and_zero_qty(session):
    record_execution(
        session, symbol="NVDA", action=Action.open, qty=10, price=100
    )
    ev = record_execution(
        session, symbol="NVDA", action=Action.close, qty=10, price=130
    )
    session.commit()

    pos = ev.position
    assert pos.status is PositionStatus.closed
    assert _D(pos.qty) == Decimal("0")
    assert pos.closed_at is not None
    # realized = (130 - 100) * 10 = 300
    assert _D(pos.realized_pnl) == Decimal("300")
    assert _D(ev.realized_pnl) == Decimal("300")
    # close event records the full remaining qty
    assert _D(ev.qty) == Decimal("10")


# ---------------------------------------------------------------------------
# Ledger: short side sign
# ---------------------------------------------------------------------------
def test_short_realized_pnl_sign(session):
    record_execution(
        session, symbol="TSLA", action=Action.open, qty=20, price=200, side=Side.short
    )
    ev = record_execution(
        session, symbol="TSLA", action=Action.close, qty=20, price=180, side="short"
    )
    session.commit()

    pos = ev.position
    assert pos.side is Side.short
    # short realized = (avg - price) * qty = (200 - 180) * 20 = 400
    assert _D(ev.realized_pnl) == Decimal("400")
    assert _D(pos.realized_pnl) == Decimal("400")
    assert pos.status is PositionStatus.closed


def test_short_loss_when_price_rises(session):
    record_execution(
        session, symbol="AMD", action=Action.open, qty=5, price=100, side=Side.short
    )
    ev = record_execution(
        session, symbol="AMD", action=Action.close, qty=5, price=120, side=Side.short
    )
    session.commit()
    # (100 - 120) * 5 = -100
    assert _D(ev.realized_pnl) == Decimal("-100")


# ---------------------------------------------------------------------------
# Ledger: fees reduce realized pnl
# ---------------------------------------------------------------------------
def test_fees_reduce_realized_pnl(session):
    record_execution(
        session, symbol="SPY", action=Action.open, qty=100, price=400
    )
    ev = record_execution(
        session, symbol="SPY", action=Action.close, qty=100, price=410, fees=25
    )
    session.commit()

    # gross = (410 - 400) * 100 = 1000; net = 1000 - 25 = 975
    assert _D(ev.realized_pnl) == Decimal("975")
    assert _D(ev.fees) == Decimal("25")
    assert _D(ev.position.realized_pnl) == Decimal("975")


# ---------------------------------------------------------------------------
# Ledger: error paths
# ---------------------------------------------------------------------------
def test_trim_no_open_position_raises(session):
    with pytest.raises(ValueError):
        record_execution(
            session, symbol="GOOG", action=Action.trim, qty=10, price=100
        )


def test_close_no_open_position_raises(session):
    with pytest.raises(ValueError):
        record_execution(
            session, symbol="GOOG", action=Action.close, qty=10, price=100
        )


def test_open_with_nonpositive_qty_raises(session):
    with pytest.raises(ValueError):
        record_execution(
            session, symbol="GOOG", action=Action.open, qty=0, price=100
        )
    with pytest.raises(ValueError):
        record_execution(
            session, symbol="GOOG", action=Action.add, qty=-5, price=100
        )


# ---------------------------------------------------------------------------
# Ledger: rebalance shares one batch_id across multiple positions
# ---------------------------------------------------------------------------
def test_rebalance_shares_batch_id_and_affects_multiple_positions(session):
    # Seed two open positions first.
    record_execution(session, symbol="AAA", action=Action.open, qty=100, price=10)
    record_execution(session, symbol="BBB", action=Action.open, qty=50, price=20)
    session.commit()

    events = rebalance(
        session,
        [
            {"symbol": "AAA", "action": Action.trim, "qty": 40, "price": 12},
            {"symbol": "BBB", "action": Action.add, "qty": 50, "price": 22},
            {"symbol": "CCC", "action": Action.open, "qty": 10, "price": 5},
        ],
        note="quarterly rebalance",
    )
    session.commit()

    assert len(events) == 3
    batch_ids = {e.batch_id for e in events}
    assert len(batch_ids) == 1
    assert next(iter(batch_ids)) is not None

    # Three distinct positions were touched.
    pos_ids = {e.position_id for e in events}
    assert len(pos_ids) == 3

    # All open: AAA trimmed to 60, BBB added to 100, CCC new at 10.
    by_symbol = {p.symbol: p for p in open_positions(session)}
    assert _D(by_symbol["AAA"].qty) == Decimal("60")
    assert _D(by_symbol["BBB"].qty) == Decimal("100")
    assert _D(by_symbol["CCC"].qty) == Decimal("10")
    # note propagated to the legs
    assert all(e.notes == "quarterly rebalance" for e in events)


# ---------------------------------------------------------------------------
# Ledger: summary + queries
# ---------------------------------------------------------------------------
def test_ledger_summary_counts_and_exposure(session):
    record_execution(session, symbol="AAA", action=Action.open, qty=100, price=10)
    record_execution(session, symbol="BBB", action=Action.open, qty=10, price=50)
    record_execution(session, symbol="BBB", action=Action.close, qty=10, price=55)
    session.commit()

    summary = ledger_summary(session)
    assert summary["open_count"] == 1  # AAA
    assert summary["closed_count"] == 1  # BBB
    # realized only on BBB: (55-50)*10 = 50
    assert summary["total_realized_pnl"] == pytest.approx(50.0)
    # exposure = open AAA qty*avg = 100*10 = 1000
    assert summary["open_exposure"] == pytest.approx(1000.0)

    assert len(all_positions(session)) == 2
    assert len(all_positions(session, status=PositionStatus.closed)) == 1
    assert len(all_positions(session, status="open")) == 1

    open_aaa = open_positions(session)[0]
    assert position_detail(session, open_aaa.id) is open_aaa
    assert position_detail(session, 999999) is None


# ---------------------------------------------------------------------------
# Archive: seed strategies from setups (idempotent)
# ---------------------------------------------------------------------------
def test_seed_strategies_from_setups_idempotent(session):
    n = archive.seed_strategies_from_setups(session)
    session.commit()
    assert n == len(SETUPS)

    strategies = archive.list_strategies(session)
    assert len(strategies) == len(SETUPS)
    slugs = {s.slug for s in strategies}
    assert slugs == set(SETUPS.keys())

    # Run again -> no duplicate slugs.
    n2 = archive.seed_strategies_from_setups(session)
    session.commit()
    assert n2 == len(SETUPS)
    strategies2 = archive.list_strategies(session)
    assert len(strategies2) == len(SETUPS)
    assert len({s.slug for s in strategies2}) == len(SETUPS)


# ---------------------------------------------------------------------------
# Archive: save_backtest persists + auto-creates strategy by slug
# ---------------------------------------------------------------------------
def test_save_backtest_persists_and_autocreates_strategy(session):
    assert archive.get_strategy(session, "brand_new") is None

    bt = archive.save_backtest(
        session,
        strategy_slug="brand_new",
        symbol="aapl",
        interval="5m",
        n_trades=42,
        win_rate=0.55,
        avg_r=0.3,
        sharpe=1.2,
        max_dd=-0.1,
        cagr=0.25,
        total_r=12.6,
        placebo_pass=True,
        start_date=dt.date(2024, 1, 1),
        end_date=dt.date(2024, 6, 1),
        params={"stop_atr": 1.0},
        equity_curve={"t": [0, 1], "v": [1.0, 1.1]},
        notes="first run",
    )
    session.commit()

    assert bt.id is not None
    assert bt.symbol == "AAPL"  # normalized
    assert bt.n_trades == 42

    # Strategy auto-created by slug.
    strat = archive.get_strategy(session, "brand_new")
    assert strat is not None
    assert bt.strategy_id == strat.id

    # The backtest is queryable.
    runs = archive.list_backtests(session, strategy_id=strat.id)
    assert len(runs) == 1
    assert runs[0].id == bt.id

    # Numeric metrics come back as Decimal.
    assert isinstance(runs[0].win_rate, Decimal)
    assert _D(runs[0].win_rate) == Decimal("0.55")


def test_save_backtest_reuses_existing_strategy(session):
    archive.upsert_strategy(session, "orb_break", name="ORB Break")
    session.commit()
    strat = archive.get_strategy(session, "orb_break")

    bt1 = archive.save_backtest(
        session, strategy_slug="orb_break", symbol="SPY", interval="5m", n_trades=1
    )
    bt2 = archive.save_backtest(
        session, strategy_slug="orb_break", symbol="QQQ", interval="5m", n_trades=2
    )
    session.commit()

    assert bt1.strategy_id == strat.id
    assert bt2.strategy_id == strat.id
    # still only one strategy
    assert len(archive.list_strategies(session)) == 1
    assert len(archive.list_backtests(session, strategy_id=strat.id)) == 2
