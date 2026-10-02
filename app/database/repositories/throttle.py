"""Cross-process publish pacing.

Telegram rate-limits a channel at roughly `CHANNEL_MESSAGES_PER_MINUTE` messages
per minute, plus a general per-chat flood control. With two processes (bot and
worker) both able to enqueue publishes, an in-memory limiter would let each of
them use the full budget and still trip the limit.

This is the database-backed alternative, and it is the *only* limiter in this
build — Redis is treated as a cache, never as the source of truth, so the pacing
has to be correct without it.

The acquire is a single conditional `UPDATE ... RETURNING`:

* it only matches when **both** the minimum interval and the window budget allow
  a send, so a row coming back means "go" and no row means "wait";
* the read-modify-write happens inside the statement, so two workers cannot both
  read `window_count = 19` and both decide they are the twentieth.

A denied caller gets the number of seconds to wait rather than a bare failure, so
the worker can reschedule the job with `defer()` instead of occupying a slot by
sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PublishThrottle
from app.logging_config import get_logger
from app.utils.timeutil import ensure_aware, utcnow

log = get_logger(__name__)

#: Returned when the statement says "wait" but the arithmetic says the wait is
#: zero. Reaching this means the two disagree — a clock skew or a rounding edge.
#: A small floor keeps the worker from spinning on a job that is always denied.
_MIN_DENIED_WAIT = 0.5


def channel_scope(channel_id: int) -> str:
    """One bucket per channel.

    Keyed by id rather than a bare `"channel"` so adding a second channel later
    is a configuration change, not a schema change.
    """
    return f"channel:{channel_id}"


class ThrottleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _ensure_row(
        self, scope: str, *, now: datetime, min_interval: float, window_seconds: float
    ) -> None:
        """Create the bucket if this is the first publish ever.

        Seeded *already expired* — `last_published_at` one interval in the past
        and the window start one window in the past — so the very first publish
        is not made to wait for a cooldown it never incurred.
        """
        stmt = (
            pg_insert(PublishThrottle)
            .values(
                scope=scope,
                last_published_at=now - timedelta(seconds=min_interval + 1),
                window_started_at=now - timedelta(seconds=window_seconds + 1),
                window_count=0,
            )
            .on_conflict_do_nothing(index_elements=[PublishThrottle.scope])
        )
        await self._session.execute(stmt)

    async def acquire(
        self,
        scope: str,
        *,
        min_interval: float,
        max_per_window: int,
        window_seconds: float,
        now: datetime | None = None,
    ) -> float:
        """Try to spend one send. Returns `0.0` if allowed, else seconds to wait.

        Calling this *commits* the send: a `0.0` return has already consumed the
        slot. Callers must therefore acquire immediately before the API call that
        needs it, not speculatively.
        """
        moment = now or utcnow()
        await self._ensure_row(
            scope, now=moment, min_interval=min_interval, window_seconds=window_seconds
        )

        interval_cutoff = moment - timedelta(seconds=min_interval)
        window_cutoff = moment - timedelta(seconds=window_seconds)
        window_resets = PublishThrottle.window_started_at <= window_cutoff

        stmt = (
            update(PublishThrottle)
            .where(
                PublishThrottle.scope == scope,
                PublishThrottle.last_published_at <= interval_cutoff,
                or_(
                    window_resets,
                    PublishThrottle.window_count < max_per_window,
                ),
            )
            .values(
                last_published_at=moment,
                window_started_at=case(
                    (window_resets, moment), else_=PublishThrottle.window_started_at
                ),
                window_count=case(
                    (window_resets, 1), else_=PublishThrottle.window_count + 1
                ),
                updated_at=moment,
            )
            .returning(PublishThrottle.scope)
        )
        allowed = (await self._session.execute(stmt)).scalar_one_or_none()
        if allowed is not None:
            return 0.0

        current = await self._session.get(
            PublishThrottle, scope, populate_existing=True
        )
        if current is None:  # pragma: no cover - _ensure_row just created it
            return 0.0

        interval_wait = (
            ensure_aware(current.last_published_at) + timedelta(seconds=min_interval)
        ) - moment
        window_wait = timedelta(0)
        if current.window_count >= max_per_window:
            window_wait = (
                ensure_aware(current.window_started_at) + timedelta(seconds=window_seconds)
            ) - moment

        wait = max(interval_wait, window_wait).total_seconds()
        if wait <= 0:
            log.warning(
                "throttle denied with a non-positive wait; applying a floor",
                extra={"scope": scope, "window_count": current.window_count},
            )
            return _MIN_DENIED_WAIT
        return wait

    async def peek(self, scope: str) -> PublishThrottle | None:
        return await self._session.get(PublishThrottle, scope)

    async def reset(self, scope: str, *, now: datetime | None = None) -> None:
        """Clear a bucket. Used by tests and by an operator unblocking a channel."""
        moment = now or utcnow()
        await self._session.execute(
            update(PublishThrottle)
            .where(PublishThrottle.scope == scope)
            .values(
                last_published_at=moment - timedelta(days=1),
                window_started_at=moment - timedelta(days=1),
                window_count=0,
            )
        )

    async def wait_time(
        self,
        scope: str,
        *,
        min_interval: float,
        max_per_window: int,
        window_seconds: float,
        now: datetime | None = None,
    ) -> float:
        """How long a send would have to wait, without consuming a slot.

        Read-only counterpart to `acquire()`, for `/status`.
        """
        moment = now or utcnow()
        stmt = select(PublishThrottle).where(PublishThrottle.scope == scope)
        row = (await self._session.execute(stmt)).scalars().first()
        if row is None:
            return 0.0
        interval_wait = (
            ensure_aware(row.last_published_at) + timedelta(seconds=min_interval)
        ) - moment
        window_wait = timedelta(0)
        if row.window_count >= max_per_window:
            window_wait = (
                ensure_aware(row.window_started_at) + timedelta(seconds=window_seconds)
            ) - moment
        return max(0.0, max(interval_wait, window_wait).total_seconds())
