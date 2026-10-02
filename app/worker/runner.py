"""The worker loop: claim, run, repeat.

## Why this shape

The loop does four things and nothing else:

1. **Beat the heartbeat.** Outside the try, unconditionally, every iteration —
   the healthcheck must see a live process even while every job slot is busy.
2. **Maintain.** Reclaim jobs from dead workers, expire stale previews, prune the
   tweet cache. On a timer rather than every pass, because each is a write and a
   loaded queue would otherwise run them many times a second.
3. **Claim.** Handled by `JobRepository.claim`, which uses
   `SELECT ... FOR UPDATE SKIP LOCKED`. Two processes — the bot's inline worker
   and a dedicated worker container — can poll the same table without blocking
   each other and without ever taking the same row.
4. **Run, concurrently, within a per-type limit.** Fetching and publishing are
   limited separately (`MAX_CONCURRENT_FETCHES`, `MAX_CONCURRENT_PUBLISHES`), so
   a burst of slow uploads cannot starve the fetches that feed them.

## What the loop deliberately does not do

It does not sleep while holding a job. A job that must wait — a publish cooldown,
Telegram flood control — raises `JobDeferral` and goes back on the queue with a
`run_after` in the future, freeing the concurrency slot immediately. Sleeping
would occupy one of the two default publish slots doing nothing.

## Cancel and crash are the same code path

On shutdown, in-flight tasks are given a grace period and then cancelled. A
cancelled job is left `running` with a stale `locked_at`, and `reclaim_expired`
returns it to the queue on a later pass. That is the same mechanism that recovers
from `docker kill`, which is why there is no separate shutdown bookkeeping here:
one recovery path, exercised constantly, rather than two of which only one is
ever tested.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from pathlib import Path

from app.config import Settings
from app.database.enums import JobType
from app.database.repositories.jobs import JobRepository, worker_identity
from app.database.repositories.submissions import SubmissionRepository
from app.database.repositories.tweets import TweetRepository
from app.database.session import session_scope
from app.logging_config import get_logger
from app.services.heartbeat import Heartbeat
from app.utils.files import prune_stale_dirs, stale_workspace_age
from app.worker.jobs import JobRunner

log = get_logger(__name__)

#: How often the maintenance pass runs. Long enough that a busy queue does not
#: pay for it on every poll, short enough that a crashed worker's jobs come back
#: promptly once `JOB_LOCK_TIMEOUT` has elapsed.
_MAINTENANCE_INTERVAL = 30.0

#: How long in-flight jobs get to finish after a shutdown signal before they are
#: cancelled and left for the reclaim sweep.
_SHUTDOWN_GRACE_SECONDS = 20.0


class Worker:
    """Owns the claim loop and the tasks it spawns."""

    def __init__(self, settings: Settings, runner: JobRunner, heartbeat: Heartbeat) -> None:
        self._settings = settings
        self._runner = runner
        self._heartbeat = heartbeat
        self._identity = worker_identity("worker")
        self._tasks: set[asyncio.Task[None]] = set()
        self._inflight: dict[JobType, int] = dict.fromkeys(JobType, 0)
        self._last_maintenance = 0.0

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def inflight(self) -> int:
        return sum(self._inflight.values())

    # ------------------------------------------------------------------ public

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set, then drain."""
        log.info(
            "worker started",
            extra={
                "identity": self._identity,
                "max_fetches": self._settings.MAX_CONCURRENT_FETCHES,
                "max_publishes": self._settings.MAX_CONCURRENT_PUBLISHES,
                "poll_interval": self._settings.WORKER_POLL_INTERVAL,
            },
        )
        try:
            while not stop.is_set():
                self._heartbeat.touch()
                await self._maintain()
                started = await self._claim_and_spawn(stop)
                if not started:
                    await self._idle(stop)
        finally:
            await self._drain()

    # -------------------------------------------------------------- claiming

    def _claimable_types(self) -> list[JobType]:
        """Which job types still have a free slot.

        An empty list means every type is saturated; the loop then waits for a
        task to finish instead of polling the database for work it could not
        start. `download_media` is never claimed — see `JobRunner._dispatch`.
        """
        limits = {
            JobType.FETCH_TWEET: self._settings.MAX_CONCURRENT_FETCHES,
            JobType.PUBLISH_TWEET: self._settings.MAX_CONCURRENT_PUBLISHES,
        }
        return [
            job_type
            for job_type, limit in limits.items()
            if self._inflight.get(job_type, 0) < limit
        ]

    async def _claim_and_spawn(self, stop: asyncio.Event) -> bool:
        """Claim at most one job. Returns whether anything was started."""
        types = self._claimable_types()
        if not types:
            await self._wait_for_capacity(stop)
            return True

        async with session_scope(self._settings) as session:
            job = await JobRepository(session).claim(
                locked_by=self._identity, types=types
            )

        if job is None:
            return False

        job_type = job.type if isinstance(job.type, JobType) else JobType(job.type)
        self._inflight[job_type] = self._inflight.get(job_type, 0) + 1

        task = asyncio.create_task(self._run_job(job.id, job_type), name=f"job:{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run_job(self, job_id: uuid.UUID, job_type: JobType) -> None:
        """Run one job, always releasing its concurrency slot."""
        try:
            await self._runner.process(job_id)
        except asyncio.CancelledError:
            # Shutdown, or a task we gave up on. The job stays `running` and the
            # reclaim sweep will return it to the queue.
            log.warning("job cancelled", extra={"job_id": str(job_id)})
            raise
        except Exception:  # noqa: BLE001 - `process` handles its own errors
            log.exception(
                "job runner raised out of its own error handling",
                extra={"job_id": str(job_id), "type": job_type.value},
            )
        finally:
            self._inflight[job_type] = max(0, self._inflight.get(job_type, 0) - 1)

    # ---------------------------------------------------------------- waiting

    async def _idle(self, stop: asyncio.Event) -> None:
        """Nothing to do: wait, but wake immediately on shutdown."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=self._settings.WORKER_POLL_INTERVAL)

    async def _wait_for_capacity(self, stop: asyncio.Event) -> None:
        """Every slot is busy. Block until one frees, or the poll interval passes.

        The timeout is not a fallback, it is the contract: this call never blocks
        longer than one poll interval, so the loop's `stop` check at the top is
        never more than that far away and shutdown stays responsive without a
        second wake-up channel. `asyncio.wait` does not cancel the tasks it
        watches, so timing out here leaves the in-flight jobs untouched.
        """
        if not self._tasks:
            # No slot free but nothing running: impossible unless a decrement was
            # lost. Wait a beat rather than spinning on the database.
            await self._idle(stop)
            return
        await asyncio.wait(
            self._tasks,
            timeout=self._settings.WORKER_POLL_INTERVAL,
            return_when=asyncio.FIRST_COMPLETED,
        )

    # ------------------------------------------------------------ maintenance

    async def _maintain(self) -> None:
        """Periodic repairs. Never fatal: the loop must survive a bad pass."""
        now = time.monotonic()
        if now - self._last_maintenance < _MAINTENANCE_INTERVAL:
            return
        self._last_maintenance = now

        try:
            async with session_scope(self._settings) as session:
                reclaimed = await JobRepository(session).reclaim_expired(
                    timeout_seconds=self._settings.JOB_LOCK_TIMEOUT
                )
                expired = await SubmissionRepository(session).expire_stale()
                pruned = await TweetRepository(session).prune_expired()
            if reclaimed or expired or pruned:
                log.info(
                    "maintenance pass",
                    extra={
                        "jobs_reclaimed": reclaimed,
                        "submissions_expired": expired,
                        "tweets_pruned": pruned,
                    },
                )
        except Exception:  # noqa: BLE001 - a failed sweep must not stop the worker
            log.exception("maintenance pass failed")

        removed = prune_stale_dirs(
            Path(self._settings.MEDIA_TMP_DIR),
            max_age_seconds=stale_workspace_age(self._settings.JOB_LOCK_TIMEOUT),
        )
        if removed:
            log.info("removed stale media workspaces", extra={"count": removed})

    # --------------------------------------------------------------- shutdown

    async def _drain(self, grace: float = _SHUTDOWN_GRACE_SECONDS) -> None:
        """Give in-flight jobs a moment, then cancel what is left."""
        if not self._tasks:
            log.info("worker stopped", extra={"identity": self._identity})
            return

        log.info(
            "waiting for in-flight jobs before stopping",
            extra={"count": len(self._tasks), "grace_seconds": grace},
        )
        done, pending = await asyncio.wait(self._tasks, timeout=grace)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        log.info(
            "worker stopped",
            extra={
                "identity": self._identity,
                "completed": len(done),
                "cancelled": len(pending),
            },
        )
