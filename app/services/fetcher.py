"""Fetching a tweet and persisting it, with the cache in front.

This is the seam between the provider layer (which knows nothing about the
database) and the repository layer (which knows nothing about HTTP). It is also
the only place that decides whether a fetch is needed at all.

Two rules it enforces:

* **A per-tweet lock is unnecessary, but a duplicate fetch is possible.** Two
  admins submitting the same link at once both enqueue a `fetch_tweet` job, and
  the second may find the cache empty. It re-fetches and upserts, which is
  idempotent — the alternative, a distributed lock, would cost more than the
  occasional duplicate API call.
* **Negative results are cached with a floor.** `TWEET_CACHE_TTL` may be 0 to
  disable caching, but a *deleted* tweet would then be re-requested on every
  attempt, which is how an account gets rate-limited. Negative entries get at
  least `_NEGATIVE_TTL_FLOOR`, and the reason is recorded either way.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.enums import MediaKind
from app.database.models import MediaItem, Tweet
from app.database.repositories.tweets import TweetRepository
from app.logging_config import get_logger
from app.providers.base import MediaData, TweetData, TweetProvider
from app.providers.base import TweetNotFoundError, TweetUnavailableError

log = get_logger(__name__)

#: Never cache a "this tweet cannot be shown" result for less than this, even
#: when `TWEET_CACHE_TTL=0`. A permanently dead link re-queried on every retry is
#: a self-inflicted rate limit.
_NEGATIVE_TTL_FLOOR = 3600

#: Keys the repository stores in `tweets.payload` alongside a provider's own
#: payload, to carry data that has no column of its own.
_URL_MAP_KEY = "_url_map"
_METRICS_KEY = "_metrics"
_AVATAR_KEY = "_author_avatar"


def to_tweet_data(tweet: Tweet, media: Sequence[MediaItem] = ()) -> TweetData:
    """Rebuild the domain object from database rows.

    Rendering, previewing and publishing all happen after the fetch, sometimes in
    a different process, so they must work from what was persisted rather than
    from anything held in memory.
    """
    payload: Mapping[str, Any] = tweet.payload if isinstance(tweet.payload, Mapping) else {}

    url_map = payload.get(_URL_MAP_KEY)
    metrics = payload.get(_METRICS_KEY)

    return TweetData(
        tweet_id=tweet.tweet_id,
        text=tweet.text or "",
        author_username=tweet.author_username,
        author_name=tweet.author_name,
        author_id=tweet.author_id,
        author_avatar_url=payload.get(_AVATAR_KEY),
        lang=tweet.lang,
        posted_at=tweet.posted_at,
        media=tuple(
            MediaData(
                position=item.position,
                kind=item.kind if isinstance(item.kind, MediaKind) else MediaKind(item.kind),
                remote_url=item.remote_url,
                content_type=item.content_type,
                width=item.width,
                height=item.height,
                duration_ms=item.duration_ms,
                size_bytes=item.size_bytes,
                alt_text=item.alt_text,
            )
            for item in media
        ),
        url_map=url_map if isinstance(url_map, Mapping) else {},
        metrics=metrics if isinstance(metrics, Mapping) else {},
        raw=payload,
    )


class TweetFetcher:
    """Provider + cache in front of `TweetRepository`."""

    def __init__(self, settings: Settings, provider: TweetProvider) -> None:
        self._settings = settings
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider.name

    async def get(
        self, session: AsyncSession, tweet_id: str, *, force: bool = False
    ) -> tuple[Tweet, bool]:
        """Return `(tweet_row, was_fetched)`.

        `force=True` skips the cache read but still writes the result, which is
        what a manual "refresh this post" would use.
        """
        repo = TweetRepository(session)

        if not force:
            cached = await repo.get_cached(tweet_id)
            if cached is not None:
                log.debug("tweet served from cache", extra={"tweet_id": tweet_id})
                return cached, False

        try:
            data = await self._provider.fetch(tweet_id)
        except TweetNotFoundError as exc:
            await self._record_unavailable(repo, tweet_id, f"not found: {exc}")
            raise
        except TweetUnavailableError as exc:
            await self._record_unavailable(repo, tweet_id, f"unavailable: {exc}")
            raise

        tweet = await repo.upsert(
            data,
            provider=self._provider.name,
            ttl_seconds=self._settings.TWEET_CACHE_TTL,
        )
        log.info(
            "fetched tweet",
            extra={
                "tweet_id": data.tweet_id,
                "provider": self._provider.name,
                "media_count": len(data.media),
            },
        )
        return tweet, True

    async def _record_unavailable(
        self, repo: TweetRepository, tweet_id: str, reason: str
    ) -> None:
        ttl = max(self._settings.TWEET_CACHE_TTL, _NEGATIVE_TTL_FLOOR)
        await repo.mark_unavailable(tweet_id, reason=reason, ttl_seconds=ttl)
        log.info(
            "tweet marked unavailable",
            extra={"tweet_id": tweet_id, "reason": reason},
        )
