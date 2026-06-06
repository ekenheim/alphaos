"""Alembic migration environment for AlphaOS.

The database URL is resolved at runtime from ``alphaos.db.engine.database_url()``
(which reads ALPHAOS_DATABASE_URL/DATABASE_URL or PG* parts) rather than being
hardcoded in ``alembic.ini``.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import the project's metadata + URL resolver.
from alphaos.db.engine import database_url
from alphaos.db.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the DB URL from the environment and inject it into the Alembic config.
_url = database_url()
if not _url:
    raise RuntimeError(
        "No database configured for Alembic. Set DATABASE_URL "
        "(or ALPHAOS_DATABASE_URL, or PGHOST/PGUSER/PGPASSWORD/PGDATABASE) "
        "from the Crunchy Postgres secret before running migrations."
    )
config.set_main_option("sqlalchemy.url", _url)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well.  By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
