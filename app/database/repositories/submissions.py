"""Pending previews — the "Publish / Cancel" state that survives a restart.

Telegram's in-memory FSM storage would be the obvious place for this, and it is
the wrong one: `docker compose restart bot` would silently drop every preview an
admin was looking at, leaving buttons that answer "unknown action". This table is
the durable version, and it works identically whether or not Redis is
configured.

Two constraints do the work:

* **Partial unique on `(admin_user_id, tweet_id)` where `status = 'pending'`** —
  an admin pasting the same link twice gets the existing preview edited, not a
  second set of buttons. It is partial so that re-submitting a tweet *after*
  publishing it is still allowed.
* **`expires_at`** — previews are swept by `expire_stale()` so the table does not
  grow without bound when admins wander off.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.enums import SubmissionStatus
from app.database.models import Submission
from app.utils.timeutil import utcnow


class SubmissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ write

    async def get_or_create(
        self,
        *,
        admin_user_id: int,
        chat_id: int,
        tweet_id: str,
        ttl_seconds: int,
        options: dict[str, Any] | None = None,
    ) -> tuple[Submission, bool]:
        """Return `(submission, created)`.

        When the same admin already has a pending preview for this tweet, the
        existing row is returned with `created=False` so the caller edits the
        preview already on screen instead of stacking a second one.

        As in `PublicationRepository.reserve`, the race is closed by attempting
        the insert inside a SAVEPOINT and treating the violation as "someone beat
        us to it" — then re-reading, because that someone is the row we want.
        """
        existing = await self.find_pending(
            admin_user_id=admin_user_id, tweet_id=tweet_id
        )
        if existing is not None:
            return existing, False

        submission = Submission(
            admin_user_id=admin_user_id,
            chat_id=chat_id,
            tweet_id=tweet_id,
            status=SubmissionStatus.PENDING,
            options=options or {},
            expires_at=utcnow() + timedelta(seconds=max(60, ttl_seconds)),
        )
        try:
            async with self._session.begin_nested():
                self._session.add(submission)
                await self._session.flush()
        except IntegrityError:
            raced = await self.find_pending(admin_user_id=admin_user_id, tweet_id=tweet_id)
            if raced is None:
                raise
            return raced, False
        return submission, True

    async def set_preview_message(self, submission: Submission, message_id: int) -> Submission:
        submission.preview_message_id = message_id
        await self._session.flush()
        return submission

    async def resolve(
        self,
        submission: Submission,
        status: SubmissionStatus,
        *,
        job_id: uuid.UUID | None = None,
    ) -> Submission:
        """Close a submission. Once resolved it stops matching the partial index,
        so the same tweet can be submitted again later."""
        submission.status = status
        submission.resolved_at = utcnow()
        if job_id is not None:
            submission.job_id = job_id
        await self._session.flush()
        return submission

    async def expire_stale(self, *, limit: int = 200) -> int:
        """Expire pending submissions past `expires_at`. Returns how many moved.

        Anything returned here is a preview whose buttons are stale; the worker's
        sweeper also strips those buttons in Telegram, which is a separate step
        because it needs the bot, not the database.
        """
        stmt = (
            select(Submission)
            .where(
                Submission.status == SubmissionStatus.PENDING,
                Submission.expires_at < utcnow(),
            )
            .order_by(Submission.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        submissions = list((await self._session.execute(stmt)).scalars())
        for submission in submissions:
            submission.status = SubmissionStatus.EXPIRED
            submission.resolved_at = utcnow()
        if submissions:
            await self._session.flush()
        return len(submissions)

    # ------------------------------------------------------------------- read

    async def get(self, submission_id: uuid.UUID) -> Submission | None:
        return await self._session.get(Submission, submission_id)

    async def find_pending(self, *, admin_user_id: int, tweet_id: str) -> Submission | None:
        stmt = select(Submission).where(
            Submission.admin_user_id == admin_user_id,
            Submission.tweet_id == tweet_id,
            Submission.status == SubmissionStatus.PENDING,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def recent(self, *, limit: int = 10) -> list[Submission]:
        stmt = select(Submission).order_by(Submission.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def pending_count(self) -> int:
        stmt = (
            select(Submission.id)
            .where(Submission.status == SubmissionStatus.PENDING)
            .limit(1000)
        )
        return len(list((await self._session.execute(stmt)).scalars()))
