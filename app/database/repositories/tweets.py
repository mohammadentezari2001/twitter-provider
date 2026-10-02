"""The tweet cache.

Tweets are cached for two reasons, and the second is the load-bearing one:

1. Provider quota is finite, and the same link gets sent more than once.
2. **`publications.tweet_id` is a foreign key into this table.** A publication
   row cannot exist without one, so a tweet is persisted before it is published
   whether or not caching is wanted. `TWEET_CACHE_TTL=0` therefore means
   "always re-fetch" (the row is written with `expires_at = fetched_at`), not
   "do not write a row" — which is why `set_ttl(0)` still inserts.

`unavailable_reason` negatively caches deletions, protected accounts and
suspended authors, so a link that cannot work is not re-requested on every
retry.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MediaItem, Publication, Tweet
from app.providers.base import TweetData
from app.utils.timeutil import utcnow


class TweetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------- read

    async def get(self, tweet_id: str) -> Tweet | None:
        """The row whatever its state, including negatively-cached failures."""
        return await self._session.get(Tweet, tweet_id)

    async def get_cached(self, tweet_id: str) -> Tweet | None:
        """The row only if it is fresh *and* not a cached negative result.

        Returning `None` for an expired row is the whole point: it pushes the
        caller to the provider. A row past `expires_at` is indistinguishable
        from a missing one, by design.
        """
        stmt = select(Tweet).where(
            Tweet.tweet_id == tweet_id,
            Tweet.expires_at > func.now(),
            Tweet.unavailable_reason.is_(None),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def media_for(self, tweet_id: str) -> list[MediaItem]:
        stmt = (
            select(MediaItem)
            .where(MediaItem.tweet_id == tweet_id)
            .order_by(MediaItem.position)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def is_referenced(self, tweet_id: str) -> bool:
        """Whether any publication points at this tweet.

        `publications.tweet_id` is `ON DELETE RESTRICT`, so the pruner has to ask
        before deleting. Asking first turns a foreign-key violation into a
        skipped row.
        """
        stmt = select(Publication.id).where(Publication.tweet_id == tweet_id).limit(1)
        return (await self._session.execute(stmt)).scalars().first() is not None

    async def count(self) -> int:
        return int((await self._session.execute(select(func.count()).select_from(Tweet))).scalar_one())

    # ------------------------------------------------------------------ write

    async def upsert(
        self,
        data: TweetData,
        *,
        provider: str,
        ttl_seconds: int,
    ) -> Tweet:
        """Insert or refresh a tweet and replace its media rows.

        The media replacement is a delete-then-insert rather than a diff. Media
        order and membership change wholesale when a tweet is re-fetched, and the
        `UNIQUE (tweet_id, position)` constraint makes a partial update fiddly for
        no benefit — a tweet has a handful of attachments at most.

        A re-fetch clears `unavailable_reason`: the tweet answered this time, so
        any earlier negative result is stale by definition.
        """
        now = utcnow()
        expires_at = now + timedelta(seconds=max(0, ttl_seconds))

        # Rendering happens from the database row, long after the provider call,
        # so anything the renderer needs must survive the round trip. The tweet
        # columns cover the post itself; these three live in `payload` because
        # they have no column of their own and are read back by `to_tweet_data`.
        payload = dict(data.raw)
        if data.url_map:
            payload["_url_map"] = dict(data.url_map)
        if data.metrics:
            payload["_metrics"] = dict(data.metrics)
        if data.author_avatar_url:
            payload["_author_avatar"] = data.author_avatar_url

        values = {
            "tweet_id": data.tweet_id,
            "author_username": data.author_username,
            "author_name": data.author_name,
            "author_id": data.author_id,
            "text": data.text,
            "lang": data.lang,
            "posted_at": data.posted_at,
            "fetched_at": now,
            "expires_at": expires_at,
            "provider": provider,
            "payload": payload,
            "media_count": len(data.media),
            "unavailable_reason": None,
        }
        stmt = pg_insert(Tweet).values(**values).on_conflict_do_update(
            index_elements=[Tweet.tweet_id],
            set_={k: v for k, v in values.items() if k != "tweet_id"},
        )
        await self._session.execute(stmt)

        await self._session.execute(
            delete(MediaItem)
            .where(MediaItem.tweet_id == data.tweet_id)
            .execution_options(synchronize_session=False)
        )
        for item in data.media:
            self._session.add(
                MediaItem(
                    tweet_id=data.tweet_id,
                    position=item.position,
                    kind=item.kind,
                    remote_url=item.remote_url,
                    content_type=item.content_type,
                    width=item.width,
                    height=item.height,
                    duration_ms=item.duration_ms,
                    size_bytes=item.size_bytes,
                    alt_text=item.alt_text,
                )
            )
        await self._session.flush()

        tweet = await self._session.get(Tweet, data.tweet_id, populate_existing=True)
        assert tweet is not None  # the upsert just guaranteed the row exists
        return tweet

    async def mark_unavailable(
        self, tweet_id: str, *, reason: str, ttl_seconds: int
    ) -> Tweet | None:
        """Record that this tweet cannot be retrieved, so we stop asking.

        The row is created if absent, because the caller may be a fetch job whose
        tweet was deleted before we ever cached it. Returns the row, or `None` if
        the tweet is unknown *and* could not be created (which cannot happen
        today, but keeps the signature honest).
        """
        now = utcnow()
        values = {
            "tweet_id": tweet_id,
            "text": "",
            "fetched_at": now,
            "expires_at": now + timedelta(seconds=max(0, ttl_seconds)),
            "provider": "unavailable",
            "payload": {},
            "media_count": 0,
            "unavailable_reason": reason,
        }
        stmt = pg_insert(Tweet).values(**values).on_conflict_do_update(
            index_elements=[Tweet.tweet_id],
            set_={
                "fetched_at": now,
                "expires_at": values["expires_at"],
                "unavailable_reason": reason,
                "media_count": 0,
            },
        )
        await self._session.execute(stmt)
        await self._session.execute(
            delete(MediaItem)
            .where(MediaItem.tweet_id == tweet_id)
            .execution_options(synchronize_session=False)
        )
        return await self._session.get(Tweet, tweet_id, populate_existing=True)

    async def record_cached_media(
        self,
        media_item: MediaItem,
        *,
        path: str,
        size_bytes: int,
        sha256: str | None,
        cache_expires_at: datetime | None,
    ) -> None:
        """Point a media row at its downloaded copy."""
        media_item.cached_path = path
        media_item.cached_size_bytes = size_bytes
        media_item.sha256 = sha256
        media_item.cached_at = utcnow()
        media_item.cache_expires_at = cache_expires_at
        await self._session.flush()

    async def prune_expired(self, *, limit: int = 500) -> int:
        """Delete stale cache rows that nothing references. Returns the count.

        Referenced tweets are skipped rather than deleted: `publications.tweet_id`
        is `ON DELETE RESTRICT`, and a publication whose tweet vanished would take
        the duplicate-suppression record with it. Cache rows are cheap; audit rows
        are not.
        """
        stmt = (
            select(Tweet.tweet_id)
            .where(Tweet.expires_at < func.now())
            .order_by(Tweet.expires_at)
            .limit(limit)
        )
        candidates = list((await self._session.execute(stmt)).scalars())
        if not candidates:
            return 0

        referenced = set(
            (
                await self._session.execute(
                    select(Publication.tweet_id).where(Publication.tweet_id.in_(candidates))
                )
            ).scalars()
        )
        removable = [tid for tid in candidates if tid not in referenced]
        if not removable:
            return 0

        await self._session.execute(
            delete(Tweet).where(Tweet.tweet_id.in_(removable)).execution_options(
                synchronize_session=False
            )
        )
        return len(removable)
