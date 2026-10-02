"""Container healthcheck.

Run as `python -m app.healthcheck` by the `HEALTHCHECK` instruction in the
Dockerfile. Exits 0 when the process is healthy, 1 otherwise, printing the reason
to stderr so `docker inspect` shows something more useful than "unhealthy".

Two things are checked, and both are needed:

1. **The heartbeat file is fresh.** `HEARTBEAT_FILE` is touched periodically by
   whichever process is running. This is what catches a container that is alive
   but wedged — an event loop blocked on a call that will never return, or
   polling that has silently stopped. `docker ps` cannot see either: the process
   still exists and the container still says "Up".
2. **The database answers.** A bot that cannot reach PostgreSQL cannot record a
   publication, and the duplicate-suppression index is the only thing stopping a
   retry from double-posting.

Deliberately *not* checked: whether Telegram is reachable. Telegram outages are
not this container's fault, and failing the healthcheck would restart the bot in
a loop precisely when restarting cannot help.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from app.config import ConfigurationError, load_settings_or_report
from app.database.session import check_database_connection, dispose_engine


def heartbeat_age_seconds(path: Path) -> float | None:
    """Seconds since the heartbeat was last touched, or `None` if it is missing."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


async def _check() -> int:
    try:
        settings = load_settings_or_report()
    except ConfigurationError as exc:
        # The message never echoes field values, so this is safe to print.
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1

    age = heartbeat_age_seconds(settings.HEARTBEAT_FILE)
    if age is None:
        print(
            f"unhealthy: heartbeat file {settings.HEARTBEAT_FILE} does not exist "
            "(has the process finished starting?)",
            file=sys.stderr,
        )
        return 1
    if age > settings.HEARTBEAT_MAX_AGE:
        print(
            f"unhealthy: heartbeat is {age:.0f}s old, over the "
            f"{settings.HEARTBEAT_MAX_AGE}s limit — the process is running but stuck",
            file=sys.stderr,
        )
        return 1

    try:
        health = await check_database_connection(settings=settings)
    finally:
        await dispose_engine()

    if not health.ok:
        print(f"unhealthy: database unreachable ({health.error})", file=sys.stderr)
        return 1

    print(f"healthy: heartbeat {age:.0f}s old, database replied in {health.latency_ms}ms")
    return 0


def main() -> int:
    try:
        return asyncio.run(_check())
    except Exception as exc:  # noqa: BLE001 - a healthcheck reports, never raises
        print(f"unhealthy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
