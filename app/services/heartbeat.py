"""The liveness signal the container healthcheck reads.

A container can be "Up" while its event loop is wedged — a call that will never
return, or polling that silently stopped. Neither shows up in `docker ps`, and
neither is fixed by a healthcheck that only asks "is the process running?".

So each long-running process touches a file on a timer, and
`app/healthcheck.py` fails when the file goes stale. The interval must be well
under `HEARTBEAT_MAX_AGE` (default 120s) or a healthy process will flap; a
quarter of it is a reasonable rule and is what `interval_for()` returns.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from app.logging_config import get_logger
from app.utils.timeutil import utcnow

log = get_logger(__name__)


def interval_for(max_age_seconds: int) -> float:
    """How often to beat so a healthy process never looks stale.

    A quarter of the limit: three consecutive missed beats are needed before the
    healthcheck complains, which absorbs a slow disk or a busy loop without
    producing a false alarm.
    """
    return max(5.0, max_age_seconds / 4.0)


class Heartbeat:
    """Touches `path` on a schedule, and can be touched by hand from a job loop."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._warned = False

    @property
    def path(self) -> Path:
        return self._path

    def touch(self) -> None:
        """Write the current time. Never raises.

        A heartbeat failure must not take down the process it is reporting on —
        an unwritable volume is a deployment mistake, and the healthcheck will
        report it as stale rather than the app crashing with a traceback about
        the wrong thing.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # The content is for a human reading the file; the healthcheck uses
            # the mtime. Writing both keeps `cat` useful.
            self._path.write_text(utcnow().isoformat(), encoding="utf-8")
        except OSError as exc:
            if not self._warned:
                # Logged once: at one beat per 30s, an unwritable path would
                # otherwise fill the log with the same line.
                log.warning(
                    "could not write the heartbeat file",
                    extra={"path": str(self._path), "error": str(exc)},
                )
                self._warned = True

    async def run(self, interval: float, stop: asyncio.Event) -> None:
        """Beat until `stop` is set. Beats once immediately.

        The immediate beat matters: without it a freshly started container is
        "unhealthy" until the first interval elapses, and Compose's
        `start_period` would have to cover the gap.
        """
        self.touch()
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                # Waiting on the event rather than sleeping means shutdown is
                # immediate instead of adding up to one interval to every restart.
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                return
            self.touch()
