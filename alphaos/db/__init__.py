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
    CashFlow,
    CashFlowKind,
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
from .cash_flows import (
    add_cash_flow,
    delete_cash_flow,
    list_cash_flows,
    net_flow_between,
    replace_avanza_cashflows,
)
from .reconcile import account_cash, account_position

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
    "CashFlow",
    "CashFlowKind",
    "NavSnapshot",
    "DeleverStatus",
    "add_cash_flow",
    "delete_cash_flow",
    "list_cash_flows",
    "net_flow_between",
    "replace_avanza_cashflows",
    "account_cash",
    "account_position",
]
