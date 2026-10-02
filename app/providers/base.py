"""Provider-neutral tweet retrieval.

The rest of the application never talks to a provider directly: it asks this
interface for a `TweetData` and gets either that or a `ProviderError` carrying a
`retryable` flag. That is what lets the same worker code sit on top of the
official X API, a licensed third-party API, or the fixture provider used in
tests, with no branching anywhere downstream.

This module holds both the **domain types** (`TweetData`, `MediaData`) and the
**interface** (`TweetProvider`). Keeping the types here rather than in the
database layer is deliberate — they are what a provider *returns*, and the
repositories import them to persist. No I/O happens at import time.

Adding a provider means adding one module and one `PROVIDER_REGISTRY` entry; see
`app/providers/__init__.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from app.database.enums import MediaKind
from app.utils.urls import canonical_tweet_url


# --------------------------------------------------------------------- errors
class ProviderError(Exception):
    """Base class for every provider failure.

    `retryable` is the contract the worker reads. It exists so that "the tweet
    was deleted" and "the API returned 503" produce different behaviour without
    either the provider or the worker knowing about the other's internals.
    """

    #: Whether retrying the identical request could plausibly succeed.
    retryable: bool = True

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} (status={self.status})" if self.status else base


class TweetNotFoundError(ProviderError):
    """No such tweet: wrong ID, deleted, or never existed.

    Not retryable — the answer will not change. Distinct from
    `TweetUnavailableError` because the operator-facing message differs.
    """

    retryable = False


class TweetUnavailableError(ProviderError):
    """The tweet exists but cannot be shown: protected, suspended, withheld,
    or from a deleted account. Not retryable for the same reason."""

    retryable = False


class ProviderAuthError(ProviderError):
    """Credentials are missing, wrong, or lack access to this endpoint.

    Not retryable. Retrying a 401 just burns quota and hides the real problem,
    which is always configuration.
    """

    retryable = False


class ProviderRateLimitedError(ProviderError):
    """Too many requests. Retryable, and the provider usually says when."""

    retryable = True


class ProviderTransientError(ProviderError):
    """Timeouts, connection resets, 5xx. Retryable."""

    retryable = True


class ProviderResponseError(ProviderError):
    """The response arrived but did not look like the documented shape.

    Treated as retryable: a truncated body or a momentary gateway page is
    indistinguishable from a genuine schema change, and the retry budget bounds
    the cost of being wrong. A persistent mismatch exhausts attempts and lands
    in `dead`, where it is visible.
    """

    retryable = True


# ------------------------------------------------------------------ data types
@dataclass(frozen=True, slots=True)
class MediaData:
    """One attached photo, video or GIF.

    `remote_url` is where the bytes can be fetched from *now*. Provider CDN URLs
    are typically signed and expire, so this is never treated as permanent —
    `media_items.cached_path` is what survives.

    A video's `remote_url` is the chosen MP4 rendition, not the HLS manifest:
    Telegram uploads a file, and only the MP4 is one. An entry whose
    `remote_url` is empty is a video the provider described but offered no
    playable rendition for; the renderer counts it as omitted rather than
    trying to fetch nothing.
    """

    position: int
    kind: MediaKind
    remote_url: str
    content_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    size_bytes: int | None = None
    alt_text: str | None = None

    @property
    def is_photo(self) -> bool:
        return self.kind is MediaKind.PHOTO


@dataclass(frozen=True, slots=True)
class TweetData:
    """A normalised tweet. Provider-agnostic by construction.

    Only `tweet_id` and `text` are required. Every provider we can realistically
    talk to omits *something* — third-party APIs rarely return a profile image,
    and the official API omits `author_username` unless you ask for the
    expansion — so the optional fields degrade to their absence in the rendered
    post instead of failing the fetch.
    """

    tweet_id: str
    text: str
    author_username: str | None = None
    author_name: str | None = None
    author_id: str | None = None
    author_avatar_url: str | None = None
    lang: str | None = None
    posted_at: datetime | None = None
    media: tuple[MediaData, ...] = ()
    #: `t.co` short link → what it actually points at, for display.
    url_map: Mapping[str, str] = field(default_factory=dict)
    #: Likes/replies/reposts, when the provider offers them.
    metrics: Mapping[str, int] = field(default_factory=dict)
    #: The normalised provider payload, stored in `tweets.payload` for debugging
    #: and for rendering fields added later without another API call. Never
    #: contains credentials — providers strip their own auth echoes.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def canonical_url(self) -> str:
        return canonical_tweet_url(self.tweet_id, self.author_username)

    @property
    def photos(self) -> tuple[MediaData, ...]:
        return tuple(m for m in self.media if m.is_photo)

    @property
    def videos(self) -> tuple[MediaData, ...]:
        """Video attachments, in the tweet's order.

        Animation is deliberately excluded: this build publishes video, and a
        GIF that a provider hands over as an `animated_gif` is still counted as
        omitted rather than silently sent as a video.
        """
        return tuple(m for m in self.media if m.kind is MediaKind.VIDEO)

    @property
    def has_unsupported_media(self) -> bool:
        """True when the tweet carries media this build does not publish.

        Surfaced in the preview so an admin is never quietly shown a post that
        will lose half its content. Video is published, so only animation (and
        any media without a playable URL) answers True here.
        """
        return any(m.kind is not MediaKind.PHOTO and m.kind is not MediaKind.VIDEO for m in self.media)


# ------------------------------------------------------------------ interface
class TweetProvider(ABC):
    """Retrieves a single tweet by ID.

    Implementations must translate *every* transport-level failure into a
    `ProviderError` subclass and must never raise anything else — the worker
    relies on `retryable` to decide between backing off and giving up, and an
    escaping `aiohttp.ClientError` would be classified by accident rather than
    by intent.
    """

    #: Stable identifier persisted in `tweets.provider`. Changing a provider's
    #: value invalidates nothing, but it does make historical rows ambiguous,
    #: so treat it as part of the config surface.
    name: str = "unnamed"

    @abstractmethod
    async def fetch(self, tweet_id: str) -> TweetData:
        """Return the tweet, or raise a `ProviderError`.

        Must be safe to call concurrently and must not cache internally —
        caching is `app/services/fetcher.py`'s job, so that `TWEET_CACHE_TTL`
        means the same thing regardless of provider.
        """

    async def aclose(self) -> None:
        """Release any held connections. Called once at shutdown."""
        return None

    async def __aenter__(self) -> TweetProvider:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
