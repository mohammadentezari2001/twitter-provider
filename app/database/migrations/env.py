"""Alembic environment.

The database URL is read from `Settings`, never from `alembic.ini`. The ini file
keeps only a placeholder so Alembic can parse it, which means the real DSN — and
any password inside it — never lands in a file that could be committed.

Migrations run through an async engine because the application driver is
`asyncpg`; there is no synchronous driver in the dependency set to fall back on.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings

# Imported for the side effect of registering every table on `Base.metadata`.
# Autogenerate cannot diff a model it has never imported, and the failure mode is
# a silently incomplete migration rather than a loud error — so this import is
# load-bearing even though nothing here refers to it.
from app.database import models  # noqa: F401
from app.database.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: `compare_server_default` is deliberately left off. PostgreSQL normalises
#: default expressions (`'{}'::jsonb`, `now()`, `true`) in ways Alembic does not
#: always round-trip, so enabling it makes `alembic check` report spurious drift
#: and trains the operator to ignore it. Type and nullability drift — the changes
#: that actually break queries — are still detected.
_CONFIGURE_KWARGS = {
    "target_metadata": target_metadata,
    "compare_type": True,
}


def _database_url() -> str:
    """Resolve the DSN, escaping `%` for ConfigParser.

    `alembic.config.Config` is backed by ConfigParser, which performs `%`
    interpolation on any value passed to `set_main_option`. A URL-encoded
    password — a `%40` standing in for `@`, say — would otherwise raise
    `InterpolationSyntaxError` before a connection is ever attempted.
    """
    return get_settings().DATABASE_URL.replace("%", "%%")


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_CONFIGURE_KWARGS,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_CONFIGURE_KWARGS)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # `NullPool` because a migration is a short, one-shot job: pooling
    # connections during DDL buys nothing and can hold locks open past exit.
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    # Injected here rather than in alembic.ini, and escaped for ConfigParser.
    config.set_main_option("sqlalchemy.url", _database_url())
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
