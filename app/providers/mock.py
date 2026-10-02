"""Fixture provider. **Development and tests only — never production.**

Selected explicitly with `TWEET_PROVIDER=mock`, never by default. It exists so
that the whole pipeline — URL handler, queue, preview, publish, stats — can be
exercised end to end without an API credential, and so the test suite has a
deterministic provider to assert against.

Fixture tweet IDs are single digits, chosen so they are impossible to confuse
with a real tweet ID (real ones are 18–19 digits). Any *other* ID returns a
synthesised tweet, so a developer can paste any link and get something back.

Media URLs point at `pbs.twimg.com` / `video.twimg.com`, which are on the default
`MEDIA_ALLOWED_HOSTS` list but will 404 in reality. That is deliberate: the mock
verifies the *pipeline*, not the CDN. A download failure therefore exercises
`MEDIA_FAILURE_MODE` rather than pretending the photo or video path works. Those
paths themselves are covered by tests that stub the downloader.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.database.enums import MediaKind
from app.providers.base import (
    MediaData,
    ProviderRateLimitedError,
    ProviderTransientError,
    TweetData,
    TweetNotFoundError,
    TweetProvider,
    TweetUnavailableError,
)
from app.utils.text import collapse_blank_lines

#: Tweet IDs with special behaviour, so tests and manual checks can trigger each
#: branch by pasting a specific link.
NOT_FOUND_ID = "5"
UNAVAILABLE_ID = "6"
RATE_LIMITED_ID = "7"
TRANSIENT_ID = "8"

_FIXED_TIME = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)

_LONG_TEXT = (
    "A thread on why long posts are the interesting case.\n\n"
    "Telegram caps a media caption at 1024 characters, which is shorter than a "
    "lot of posts people actually write. When the text does not fit, something "
    "has to give: either the caption is truncated, or the text is sent as its own "
    "message first and the media follows. This fixture exists so that both paths "
    "can be exercised without hunting for a real post that happens to be long "
    "enough — which is harder than it sounds, because the interesting threshold is "
    "not 280 characters but 1024, and relatively few posts reach it.\n\n"
    "The extension is here purely to push the total past the caption limit while "
    "remaining human-readable. Padding with lorem ipsum would work, but a fixture "
    "that reads like a real post makes an unexpected rendering immediately obvious "
    "rather than something you have to squint at.\n\n"
    "One more paragraph, because the counter needs to clear 1024 comfortably rather "
    "than landing a few characters short and leaving the branch untested."
)


def _photo(position: int, name: str, *, alt: str | None = None) -> MediaData:
    return MediaData(
        position=position,
        kind=MediaKind.PHOTO,
        remote_url=f"https://pbs.twimg.com/media/{name}.jpg",
        content_type="image/jpeg",
        width=1200,
        height=675,
        alt_text=alt,
    )


def _fixtures() -> dict[str, TweetData]:
    return {
        # Text only — the simplest path, no media group involved.
        "1": TweetData(
            tweet_id="1",
            text="Text-only fixture. No media, so the caption limit is 4096 and "
            "the whole post fits in one message.",
            author_username="fixture_dev",
            author_name="Fixture Dev",
            author_id="1001",
            lang="en",
            posted_at=_FIXED_TIME,
            metrics={"like_count": 12, "retweet_count": 3},
            raw={"fixture": True},
        ),
        # A single photo — sendPhoto rather than sendMediaGroup.
        "2": TweetData(
            tweet_id="2",
            text="Single photo fixture.",
            author_username="fixture_dev",
            author_name="Fixture Dev",
            author_id="1001",
            lang="en",
            posted_at=_FIXED_TIME + timedelta(hours=1),
            media=(_photo(0, "FIXTURE1", alt="A fixture image"),),
            raw={"fixture": True},
        ),
        # Three photos — sendMediaGroup.
        "3": TweetData(
            tweet_id="3",
            text="Three photos, which is the smallest interesting media group.",
            author_username="fixture_dev",
            author_name="Fixture Dev",
            author_id="1001",
            lang="en",
            posted_at=_FIXED_TIME + timedelta(hours=2),
            media=(
                _photo(0, "FIXTURE2", alt="First"),
                _photo(1, "FIXTURE3", alt="Second"),
                _photo(2, "FIXTURE4", alt="Third"),
            ),
            url_map={"https://t.co/fixture": "https://example.com/a-long-link"},
            raw={"fixture": True},
        ),
        # Long text plus media — the caption-overflow path.
        "4": TweetData(
            tweet_id="4",
            text=_LONG_TEXT,
            author_username="fixture_dev",
            author_name="Fixture Dev",
            author_id="1001",
            lang="en",
            posted_at=_FIXED_TIME + timedelta(hours=3),
            media=(_photo(0, "FIXTURE5"), _photo(1, "FIXTURE6")),
            raw={"fixture": True},
        ),
        # A single video — the shape X returns for a video or GIF post.
        "9": TweetData(
            tweet_id="9",
            text="Video fixture — one MP4 attachment, published as a playable clip.",
            author_username="fixture_dev",
            author_name="Fixture Dev",
            author_id="1001",
            lang="en",
            posted_at=_FIXED_TIME + timedelta(hours=4),
            media=(
                MediaData(
                    position=0,
                    kind=MediaKind.VIDEO,
                    remote_url="https://video.twimg.com/fixture/clip.mp4",
                    content_type="video/mp4",
                    width=1280,
                    height=720,
                    duration_ms=15_000,
                ),
            ),
            raw={"fixture": True},
        ),
    }


class MockProvider(TweetProvider):
    """Deterministic fixture provider."""

    name = "mock"

    def __init__(self) -> None:
        self._fixtures = _fixtures()
        self.fetches: list[str] = []

    async def fetch(self, tweet_id: str) -> TweetData:
        self.fetches.append(tweet_id)

        if tweet_id == NOT_FOUND_ID:
            raise TweetNotFoundError(f"fixture: tweet {tweet_id} does not exist")
        if tweet_id == UNAVAILABLE_ID:
            raise TweetUnavailableError(
                f"fixture: tweet {tweet_id} is from a protected account"
            )
        if tweet_id == RATE_LIMITED_ID:
            raise ProviderRateLimitedError(
                "fixture: rate limited", status=429, retry_after=2.0
            )
        if tweet_id == TRANSIENT_ID:
            raise ProviderTransientError("fixture: upstream is unreachable", status=503)

        known = self._fixtures.get(tweet_id)
        if known is not None:
            return known

        # Anything else gets a synthetic tweet, so any real link is usable in dev.
        return TweetData(
            tweet_id=tweet_id,
            text=collapse_blank_lines(
                f"Mock tweet {tweet_id}.\n\n"
                "This ID is not one of the fixtures, so the provider synthesised a "
                "post. Fixture IDs are 1–4 and 9; ID 5 is not-found, 6 unavailable, "
                "7 rate-limited and 8 transient."
            ),
            author_username="fixture_dev",
            author_name="Fixture Dev",
            author_id="1001",
            lang="en",
            posted_at=_FIXED_TIME,
            raw={"fixture": True, "synthesised": True},
        )
