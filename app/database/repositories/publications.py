"""What actually reached the channel, and the guard that stops it happening twice.

The duplicate-suppression rule is a **partial unique index** on
`(tweet_id, channel_id)` covering every status except `failed` and `deleted`.
Two admins pasting the same link, or an auto-publish racing a manual confirm,
therefore collapse to one row at the database level rather than depending on a
check-then-insert that two processes can interleave.

Excluding `failed` is what makes the rule humane: a publish that genuinely failed
can be retried, because there is no row claiming it succeeded. `deleted` is
excluded for the same reason — a post removed from the channel should be
postable again.

`partial` is a real outcome, not a nicety: a publish that sends the text message
and then fails on the photo has put something in the channel, so it must not be
retried as if nothing happened, and it must not count as a success either.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.enums import PublicationMode, PublicationStatus
from app.database.models import Publication
from app.utils.text import smart_truncate
from app.utils.timeutil import utcnow

_ERROR_MAX_CHARS = 2000

#: The predicate of `ix_publications_dedupe`, quoted here only so the failure
#: path can explain itself. The index is the authority.
_ACTIVE_STATUSES = (
    PublicationStatus.PENDING,
    PublicationStatus.PUBLISHED,
    PublicationStatus.PARTIAL,
)


class PublicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ write

    async def reserve(
        self,
        *,
        tweet_id: str,
        channel_id: int,
        mode: PublicationMode,
        template: str,
        media_count: int,
        job_id: uuid.UUID | None = None,
        published_by: int | None = None,
    ) -> Publication | None:
        """Claim the right to publish this tweet to this channel.

        Returns the new `pending` row, or `None` when an active publication
        already exists — which the caller must treat as "already published",
        not as an error.

        The insert is attempted rather than preceded by a `SELECT`, because only
        the index can decide this atomically. The `IntegrityError` is caught
        inside a SAVEPOINT so the surrounding transaction survives: a bare
        `except IntegrityError` would leave the session in a failed transaction
        and every subsequent statement would error with "current transaction is
        aborted".
        """
        publication = Publication(
            tweet_id=tweet_id,
            channel_id=channel_id,
            mode=mode,
            template=template,
            media_count=media_count,
            status=PublicationStatus.PENDING,
            message_ids=[],
            job_id=job_id,
            published_by=published_by,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(publication)
                await self._session.flush()
        except IntegrityError:
            return None
        return publication

    async def mark_published(
        self,
        publication: Publication,
        *,
        message_ids: Sequence[int],
        primary_message_id: int | None,
        message_url: str | None,
        caption: str,
        media_sent: int,
        status: PublicationStatus = PublicationStatus.PUBLISHED,
    ) -> Publication:
        publication.message_ids = list(message_ids)
        publication.primary_message_id = primary_message_id
        publication.message_url = message_url
        publication.caption = caption
        publication.caption_length = len(caption)
        publication.media_sent = media_sent
        publication.status = status
        publication.published_at = utcnow()
        publication.error = None
        await self._session.flush()
        return publication

    async def mark_failed(self, publication: Publication, error: str) -> Publication:
        """Mark the attempt failed.

        The row stays `failed` rather than being deleted, so the operator can see
        what was tried — and because `failed` is outside the dedupe index, it
        stops blocking a retry.
        """
        publication.status = PublicationStatus.FAILED
        publication.error = smart_truncate(error, _ERROR_MAX_CHARS)
        await self._session.flush()
        return publication

    async def mark_deleted(self, publication: Publication, *, message_id: int) -> Publication:
        publication.status = PublicationStatus.DELETED
        publication.message_ids = [
            mid for mid in (publication.message_ids or []) if mid != message_id
        ]
        await self._session.flush()
        return publication

    # ------------------------------------------------------------------- read

    async def get(self, publication_id: uuid.UUID) -> Publication | None:
        return await self._session.get(Publication, publication_id)

    async def find_active(self, *, tweet_id: str, channel_id: int) -> Publication | None:
        """The publication currently blocking a re-post, if any."""
        stmt = select(Publication).where(
            Publication.tweet_id == tweet_id,
            Publication.channel_id == channel_id,
            Publication.status.in_(_ACTIVE_STATUSES),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def recent(
        self, *, limit: int = 10, channel_id: int | None = None
    ) -> list[Publication]:
        stmt = select(Publication).order_by(Publication.created_at.desc()).limit(limit)
        if channel_id is not None:
            stmt = stmt.where(Publication.channel_id == channel_id)
        return list((await self._session.execute(stmt)).scalars())

    async def counts_since(self, since: datetime, *, channel_id: int | None = None) -> dict[str, int]:
        stmt = (
            select(Publication.status, func.count())
            .where(Publication.created_at >= since)
            .group_by(Publication.status)
        )
        if channel_id is not None:
            stmt = stmt.where(Publication.channel_id == channel_id)
        rows = (await self._session.execute(stmt)).all()
        return {status.value: count for status, count in rows}
