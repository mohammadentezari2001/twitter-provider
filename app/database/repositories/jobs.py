"""The durable job queue.

Everything that talks to the network runs as a job here rather than inline in a
Telegram handler: a handler that fetches a tweet blocks the bot's update loop,
and a handler that publishes blocks it for as long as the upload takes. Jobs also
give retries, backoff and crash recovery for free.

Three mechanisms carry the weight:

* **`claim()`** uses `SELECT ... FOR UPDATE SKIP LOCKED`, so the bot process and
  the worker process can both poll the same table without ever taking the same
  row. This is the whole reason the queue can live in PostgreSQL instead of
  needing a broker.
* **The partial unique index on `dedupe_key`** (over `pending`/`running` only)
  makes "enqueue unless one is already in flight" a database guarantee rather
  than a check-then-insert race.
* **`reclaim_expired()`** returns rows whose worker died mid-job to `pending`.
  Without it a `docker kill` would strand those jobs forever, because nothing
  else ever moves a row out of `running`.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.enums import JobStatus, JobType
from app.database.models import DEFAULT_MAX_ATTEMPTS, Job
from app.utils.retry import next_attempt_at
from app.utils.text import smart_truncate
from app.utils.timeutil import utcnow

#: Errors are stored for operators to read, not replayed. A chained
#: `httpx`/`asyncpg` traceback can run to tens of kilobytes and there is nothing
#: useful past the first few hundred characters.
_ERROR_MAX_CHARS = 2000


@dataclass(frozen=True, slots=True)
class QueueDepth:
    """Counts by status, for `/status` and the health endpoint."""

    pending: int = 0
    running: int = 0
    failed: int = 0
    dead: int = 0

    @property
    def backlog(self) -> int:
        """Jobs that still want to run. `dead` is excluded: it needs a human."""
        return self.pending

    def as_dict(self) -> dict[str, int]:
        return {
            "pending": self.pending,
            "running": self.running,
            "failed": self.failed,
            "dead": self.dead,
        }


def worker_identity(prefix: str = "worker") -> str:
    """A stable-ish name for `jobs.locked_by`.

    Hostname plus PID. In Compose the hostname is the container id, so two
    replicas never collide, and after a restart the old value can still be told
    apart from the new one while `reclaim_expired()` cleans up.
    """
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}"


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ write

    async def enqueue(
        self,
        *,
        type: JobType,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        priority: int = 0,
        run_after: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        requested_by: int | None = None,
        chat_id: int | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> tuple[Job | None, bool]:
        """Add a job. Returns `(job, created)`.

        With a `dedupe_key`, a second enqueue while an identical job is still
        `pending` or `running` returns the existing row and `created=False`, so
        the caller can tell the admin "already in progress" instead of queueing
        the work twice.

        The conflict is detected by *attempting the insert* and catching the
        unique violation inside a SAVEPOINT, rather than by selecting first. A
        select-then-insert would race between two processes; the savepoint keeps
        the surrounding transaction usable after the violation, which a bare
        `except IntegrityError` would not.

        `job` is `None` only in the narrow case where the conflicting row left
        `pending`/`running` between our insert and the follow-up lookup. The
        caller should treat that as "duplicate, skip" — the work is either done
        or owned by something else.
        """
        job = Job(
            type=type,
            payload=payload,
            dedupe_key=dedupe_key,
            priority=priority,
            run_after=run_after or utcnow(),
            max_attempts=max_attempts,
            requested_by=requested_by,
            chat_id=chat_id,
            status=JobStatus.PENDING,
        )
        if correlation_id is not None:
            job.correlation_id = correlation_id

        if dedupe_key is None:
            self._session.add(job)
            await self._session.flush()
            return job, True

        try:
            async with self._session.begin_nested():
                self._session.add(job)
                await self._session.flush()
        except IntegrityError:
            existing = await self.find_active_by_dedupe_key(dedupe_key)
            return existing, False
        return job, True

    async def find_active_by_dedupe_key(self, dedupe_key: str) -> Job | None:
        stmt = select(Job).where(
            Job.dedupe_key == dedupe_key,
            Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def claim(
        self, *, locked_by: str, types: Sequence[JobType] | None = None
    ) -> Job | None:
        """Take the next runnable job, or return `None`.

        `FOR UPDATE SKIP LOCKED` is what makes this safe from both processes
        concurrently: a row another transaction has locked is stepped over
        rather than waited on, so nobody blocks and no job is handed out twice.

        `attempts` is incremented here, at claim time, not at failure time. That
        way a worker killed mid-job has still consumed an attempt, which is the
        behaviour you want — a job that crashes the process must not retry
        forever.

        The caller owns the transaction; this does not commit. The `FOR UPDATE`
        lock is held until that commit, which is deliberate: it is the window
        during which the job is ours.
        """
        stmt = (
            select(Job)
            .where(
                Job.status == JobStatus.PENDING,
                Job.run_after <= func.now(),
            )
            .order_by(Job.priority.desc(), Job.run_after, Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if types:
            stmt = stmt.where(Job.type.in_(list(types)))

        job = (await self._session.execute(stmt)).scalars().first()
        if job is None:
            return None

        job.status = JobStatus.RUNNING
        job.locked_at = utcnow()
        job.locked_by = locked_by
        job.attempts += 1
        if job.started_at is None:
            job.started_at = utcnow()
        await self._session.flush()
        return job

    async def mark_succeeded(self, job: Job, *, result: dict[str, Any] | None = None) -> Job:
        job.status = JobStatus.SUCCEEDED
        job.result = result
        job.finished_at = utcnow()
        job.locked_at = None
        job.locked_by = None
        job.last_error = None
        await self._session.flush()
        return job

    async def mark_failed(
        self,
        job: Job,
        error: str,
        *,
        retryable: bool = True,
        base_delay: float = 5.0,
        max_delay: float = 900.0,
        override_delay: float | None = None,
    ) -> JobStatus:
        """Record a failure and decide whether to retry.

        Returns the status the job ended in, so the caller can log or notify
        without re-reading the row. A non-retryable failure (a deleted tweet, a
        malformed response) goes straight to `failed` without spending the
        remaining attempts — retrying something that cannot succeed only delays
        the error the operator needs to see.

        `dead` means attempts were exhausted. It is separated from `failed`
        precisely so `/status` and the ops queries can point at the jobs that
        need a human rather than the ones that were always going to fail.
        """
        job.last_error = smart_truncate(error, _ERROR_MAX_CHARS)
        job.locked_at = None
        job.locked_by = None

        if not retryable:
            job.status = JobStatus.FAILED
            job.finished_at = utcnow()
        elif job.attempts >= job.max_attempts:
            job.status = JobStatus.DEAD
            job.finished_at = utcnow()
        else:
            job.status = JobStatus.PENDING
            job.run_after = next_attempt_at(
                job.attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                override_delay=override_delay,
            )
        await self._session.flush()
        return job.status

    async def defer(self, job: Job, *, seconds: float, reason: str | None = None) -> Job:
        """Put the job back without consuming an attempt.

        For "not now" rather than "did not work" — a publish cooldown still
        ticking, or a provider asking us to back off. Counting these as
        failures would let a busy channel exhaust `max_attempts` and kill a job
        that never actually failed, so the attempt charged by `claim()` is
        handed back.
        """
        job.status = JobStatus.PENDING
        job.attempts = max(0, job.attempts - 1)
        job.run_after = utcnow() + timedelta(seconds=max(0.0, seconds))
        job.locked_at = None
        job.locked_by = None
        if reason:
            job.last_error = smart_truncate(f"deferred: {reason}", _ERROR_MAX_CHARS)
        await self._session.flush()
        return job

    async def reclaim_expired(self, *, timeout_seconds: float, limit: int = 100) -> int:
        """Return jobs from dead workers to `pending`. Returns how many moved.

        A job is only reclaimed once, and only after `timeout_seconds`, so a
        slow-but-alive publish is not stolen from the worker still running it.
        The attempt charged when it was first claimed stands: a job that kills
        its worker should not be allowed to do so indefinitely.
        """
        cutoff = utcnow() - timedelta(seconds=timeout_seconds)
        stmt = (
            select(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.locked_at.is_not(None),
                Job.locked_at < cutoff,
            )
            .order_by(Job.locked_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        jobs = list((await self._session.execute(stmt)).scalars())
        for job in jobs:
            if job.attempts >= job.max_attempts:
                job.status = JobStatus.DEAD
                job.finished_at = utcnow()
                job.last_error = smart_truncate(
                    f"worker {job.locked_by} stopped responding and attempts are exhausted",
                    _ERROR_MAX_CHARS,
                )
            else:
                job.status = JobStatus.PENDING
                job.run_after = utcnow()
                job.last_error = smart_truncate(
                    f"reclaimed from {job.locked_by} after {timeout_seconds:.0f}s",
                    _ERROR_MAX_CHARS,
                )
            job.locked_at = None
            job.locked_by = None
        if jobs:
            await self._session.flush()
        return len(jobs)

    async def cancel(self, job_id: uuid.UUID) -> bool:
        """Cancel a job that has not started. Returns whether anything changed."""
        result = await self._session.execute(
            Job.__table__.update()
            .where(Job.id == job_id, Job.status == JobStatus.PENDING)
            .values(status=JobStatus.CANCELLED, finished_at=func.now())
        )
        return bool(result.rowcount)

    # ------------------------------------------------------------------- read

    async def get(self, job_id: uuid.UUID) -> Job | None:
        return await self._session.get(Job, job_id)

    async def recent(self, *, limit: int = 10, requested_by: int | None = None) -> list[Job]:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if requested_by is not None:
            stmt = stmt.where(Job.requested_by == requested_by)
        return list((await self._session.execute(stmt)).scalars())

    async def queue_depth(self) -> QueueDepth:
        """One grouped scan instead of five counts."""
        stmt = select(Job.status, func.count()).group_by(Job.status)
        rows = (await self._session.execute(stmt)).all()
        counts = {status: count for status, count in rows}
        return QueueDepth(
            pending=counts.get(JobStatus.PENDING, 0),
            running=counts.get(JobStatus.RUNNING, 0),
            failed=counts.get(JobStatus.FAILED, 0),
            dead=counts.get(JobStatus.DEAD, 0),
        )

    async def oldest_pending_age_seconds(self) -> float | None:
        """How long the oldest queued job has been waiting.

        The single most useful number for spotting a stuck worker: if the bot is
        enqueuing and this keeps climbing, nothing is draining the queue.
        """
        stmt = select(func.min(Job.created_at)).where(Job.status == JobStatus.PENDING)
        oldest = (await self._session.execute(stmt)).scalar_one_or_none()
        if oldest is None:
            return None
        return max(0.0, (utcnow() - oldest).total_seconds())

    async def enqueued_since(self, since: datetime) -> int:
        stmt = select(func.count()).select_from(Job).where(Job.created_at >= since)
        return int((await self._session.execute(stmt)).scalar_one())
