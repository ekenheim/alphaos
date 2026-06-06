"""Database engine + session management.

Connection resolution order (first match wins):

  1. ALPHAOS_DATABASE_URL or DATABASE_URL  — a full SQLAlchemy/libpq URL.
  2. PG* parts                              — PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE.

Crunchy Postgres for Kubernetes (CPNG) publishes a Secret named
`<cluster>-pguser-<user>` with keys: host, port, dbname, user, password, uri.
Wire it into the pod either way:

  - set DATABASE_URL from the secret's `uri` key, or
  - set PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE from host/port/user/password/dbname.

Either is normalized to the psycopg3 driver here, so the raw CPNG `uri`
(`postgresql://...`) works as-is.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def _normalize_url(url: str) -> str:
    """Force the psycopg (v3) driver so a plain CPNG `postgresql://` uri works."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def database_url() -> str | None:
    """Resolve the DB URL from the environment, or None if unconfigured."""
    for key in ("ALPHAOS_DATABASE_URL", "DATABASE_URL"):
        v = os.getenv(key)
        if v:
            return _normalize_url(v)

    host = os.getenv("PGHOST") or os.getenv("ALPHAOS_PGHOST")
    if host:
        user = os.getenv("PGUSER") or os.getenv("ALPHAOS_PGUSER") or "postgres"
        pw = os.getenv("PGPASSWORD") or os.getenv("ALPHAOS_PGPASSWORD") or ""
        port = os.getenv("PGPORT") or os.getenv("ALPHAOS_PGPORT") or "5432"
        db = os.getenv("PGDATABASE") or os.getenv("ALPHAOS_PGDATABASE") or user
        auth = f"{quote_plus(user)}:{quote_plus(pw)}@" if pw else f"{quote_plus(user)}@"
        return f"postgresql+psycopg://{auth}{host}:{port}/{db}"

    return None


def have_database() -> bool:
    """True if the environment provides a usable DB connection config."""
    return database_url() is not None


class _State:
    engine: Engine | None = None
    factory: sessionmaker[Session] | None = None


_state = _State()


def get_engine() -> Engine:
    """Lazily build (and cache) the engine. Raises if no DB is configured."""
    if _state.engine is None:
        url = database_url()
        if not url:
            raise RuntimeError(
                "No database configured. Set DATABASE_URL (or PGHOST/PGUSER/... ) "
                "from the Crunchy Postgres secret."
            )
        _state.engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
        _state.factory = sessionmaker(
            bind=_state.engine, expire_on_commit=False, class_=Session
        )
    return _state.engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _state.factory is not None
    return _state.factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commits on success, rolls back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
