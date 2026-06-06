"""AlphaOS persistence layer (SQLAlchemy 2.0 + Postgres / Crunchy).

Holds relational state — strategies, backtest results, live positions, and the
trade-event ledger. Bulk OHLCV bars stay in MinIO (see alphaos/minio.py); the DB
is for transactional/relational data only.

Connection is read from the environment (Crunchy Postgres secret); see engine.py.
"""

from __future__ import annotations

from .engine import (
    database_url,
    get_engine,
    get_sessionmaker,
    have_database,
    session_scope,
)
from .models import (
    Action,
    Backtest,
    Base,
    Position,
    PositionStatus,
    Side,
    Strategy,
    StrategyStatus,
    TradeEvent,
)

__all__ = [
    "database_url",
    "get_engine",
    "get_sessionmaker",
    "have_database",
    "session_scope",
    "Base",
    "Strategy",
    "StrategyStatus",
    "Backtest",
    "Position",
    "PositionStatus",
    "Side",
    "Action",
    "TradeEvent",
]
