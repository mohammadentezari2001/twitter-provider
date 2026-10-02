"""The "importable with no services running" invariant.

`app.config` states that `Settings` must be constructible in a test process with
no network and no database. The database layer is where that is easiest to break:
one module-level `create_async_engine(...)` and importing the package would start
holding a connection pool bound to whichever event loop imported it first — which
breaks the moment a test suite creates a second loop, and makes an offline
`alembic` or CLI import attempt a connection.

Order matters within this module. The engine is a process-wide singleton, so the
laziness assertion must run before anything calls `get_engine`; the health-check
test at the end disposes the engine again in a `finally`.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.database import session


def test_settings_are_constructible_with_no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_env_file=None` only suppresses the file; real environment variables still
    # apply, so they are cleared here to make the assertion about defaults true
    # on any machine.
    monkeypatch.delenv("TIMEZONE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    built = Settings(_env_file=None)
    assert built.TIMEZONE == "UTC"
    assert built.DATABASE_URL.startswith("postgresql+asyncpg://")


def test_importing_the_database_layer_creates_no_engine() -> None:
    """Reaching into module state is the only way to assert this, and the
    invariant is worth it: an eager engine is invisible until it breaks a
    second event loop."""
    assert session._engine is None
    assert session._session_factory is None


async def test_health_check_reports_failure_instead_of_raising() -> None:
    """A probe returns a result; it never propagates.

    The password assertion is deliberate. A failed `connect()` can embed the DSN
    in its message, and the healthcheck writes that message to the log — so a
    regression that leaks credentials here would leak them into a log file that
    is read by anyone debugging a stuck container.
    """
    unreachable = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://relayuser:sup3rsecret@127.0.0.1:1/relay",
    )
    try:
        health = await session.check_database_connection(
            timeout_seconds=3.0, settings=unreachable
        )
    finally:
        await session.dispose_engine()

    assert health.ok is False
    assert health.error
    assert "sup3rsecret" not in health.error
