"""AlphaOS persistence layer (SQLAlchemy 2.0 + Postgres / Crunchy).

Holds the V2-FRONTIER portfolio state: config, sleeves + target weights, holdings,
and the NAV-index/TWR ledger. Connection is read from the environment (Crunchy
Postgres secret); see engine.py.
"""

from __future__ import annotations

from .engine import (
    database_url,
    db_status,
    get_engine,
    get_sessionmaker,
    have_database,
    session_scope,
)
from .models import (
    AssetClass,
    Base,
    DeleverStatus,
    Holding,
    NavSnapshot,
    PortfolioConfig,
    PriceSource,
    Sleeve,
    SleeveKind,
    Transaction,
    TransactionKind,
    TxnSource,
)

__all__ = [
    "database_url",
    "db_status",
    "get_engine",
    "get_sessionmaker",
    "have_database",
    "session_scope",
    "Base",
    "PortfolioConfig",
    "Sleeve",
    "SleeveKind",
    "Holding",
    "AssetClass",
    "PriceSource",
    "Transaction",
    "TransactionKind",
    "TxnSource",
    "NavSnapshot",
    "DeleverStatus",
]
