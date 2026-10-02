"""Async engine and session management.

The engine is created **lazily**, on first use, and never at import time. That is
a hard requirement rather than a style preference: `app.config` states that
`Settings` must be constructible in a test process with no services running, and
a module-level engine would also bind a connection pool to whichever event loop
happened to import the module first — which breaks the moment a test suite
creates a second loop.

Every write path goes through `session_scope()`, so transaction boundaries are
never implicit. `autoflush` is off deliberately: an implicit flush in the middle
of a handler can emit a half-built `INSERT` at an unrelated line, and the
resulting `IntegrityError` is far harder to attribute than an explicit flush.
`expire_on_commit` is off so an object returned from a committed block is still
usable without a second round-trip.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

#: 30 minutes is the usual idle cutoff applied by a managed PostgreSQL or a
#: connection proxy. Recycling just below it means we close the socket first,
#: instead of discovering it is dead on the next checkout.
_POOL_RECYCLE_SECONDS = 1800

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide engine, creating it on first call."""
    global _engine
    if _engine is None:
        config = settings or get_settings()
        _engine = create_async_engine(
            config.DATABASE_URL,
            echo=config.DB_ECHO,
            pool_size=config.DB_POOL_SIZE,
            max_overflow=config.DB_MAX_OVERFLOW,
            pool_recycle=_POOL_RECYCLE_SECONDS,
            # `pool_pre_ping` costs a round-trip per checkout but converts the
            # common "server restarted, socket is stale" failure from an
            # `InterfaceError` mid-transaction into a transparent reconnect.
            # For a bot that must survive a database restart unattended, that is
            # the right trade.
            pool_pre_ping=True,
        )
        logger.info(
            "Database engine created",
            extra={
                "pool_size": config.DB_POOL_SIZE,
                "max_overflow": config.DB_MAX_OVERFLOW,
                "echo": config.DB_ECHO,
            },
        )
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(settings),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """A transactional session: commit on success, roll back on any exception.

    `BaseException` rather than `Exception` is intentional — a cancelled task
    must still roll back, otherwise the connection returns to the pool with an
    open transaction and the next borrower inherits it.

    Do not nest these. A nested scope would commit the outer transaction at the
    inner boundary; pass the session down instead.
    """
    session = get_sessionmaker(settings)()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction the caller owns.

    This is the dependency-injection shape (aiogram middlewares, FastAPI
    `Depends`): the caller decides when to commit, which is what a handler that
    both reads and writes around an `await` to Telegram needs.
    """
    async with get_sessionmaker(settings)() as session:
        yield session


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """Result of a liveness probe, shaped for a container healthcheck."""

    ok: bool
    latency_ms: int | None = None
    error: str | None = None


async def check_database_connection(
    *, timeout_seconds: float = 5.0, settings: Settings | None = None
) -> DatabaseHealth:
    """Run `SELECT 1` under a hard deadline.

    The parameter is `timeout_seconds` rather than `timeout` because
    `ruff`'s ASYNC109 flags the bare name, and the deadline is applied with
    `asyncio.timeout` exactly as that rule intends.

    `settings` is accepted so a probe can be aimed at an explicit DSN — that is
    what lets the "unreachable database reports rather than raises" path be
    tested without one running.

    Never raises: the caller is a healthcheck, and "the database is unreachable"
    is a result, not an exception. The error text is a type name plus message and
    is passed through the redaction filter like any other log line — a failed
    connect() can otherwise embed the DSN, and therefore the password.
    """
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            async with get_engine(settings).connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("Database health check failed", extra={"error": detail})
        return DatabaseHealth(ok=False, error=detail)
    return DatabaseHealth(
        ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
    )


async def dispose_engine() -> None:
    """Close the pool and forget the engine.

    Call on shutdown so in-flight queries drain and PostgreSQL does not have to
    reap the connections itself. Resetting the module globals also makes this the
    hook a test fixture uses between event loops.
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _session_factory = None
