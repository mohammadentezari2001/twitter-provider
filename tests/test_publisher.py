"""Sending a rendered post to Telegram.

`Publisher._send` is where the message plan becomes actual API calls, and it is
tested directly — with a fake bot rather than a fake transport — because the
decisions it makes are the interesting part:

* a video is uploaded with `sendVideo` (a playable clip), not posted as a URL and
  not sent as a document;
* the caption rides the *first* media message and no other, since Telegram
  ignores a caption on a second one — attaching it to each would be code that
  silently does nothing;
* a post that lost its media is text, and is reported as `partial` rather than
  `published`, so the record matches what the channel actually shows;
* an omitted video (an HLS-only rendition, or `ALLOW_VIDEO=false`) is a counted
  omission, not a download failure.

The publish path around this — throttle, download, database record — is
exercised by the worker tests and by the integration suite, both of which need
services this file deliberately avoids.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from aiogram.types import FSInputFile

from app.config import LongCaptionMode, Settings
from app.database.enums import MediaKind, PublicationMode, PublicationStatus
from app.database.models import Publication
from app.providers.base import MediaData, TweetData
from app.services.media import DownloadedMedia
from app.services.publisher import Publisher
from app.services.render import render_post

CHANNEL_ID = -1001234567890


# ---------------------------------------------------------------------- fakes
class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    """Records the API calls a publish makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 100

    def _record(self, method: str, kwargs: dict[str, Any]) -> FakeMessage:
        self.calls.append((method, kwargs))
        self._next_id += 1
        return FakeMessage(self._next_id)

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_for(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    async def send_message(self, **kwargs: Any) -> FakeMessage:
        return self._record("send_message", kwargs)

    async def send_photo(self, **kwargs: Any) -> FakeMessage:
        return self._record("send_photo", kwargs)

    async def send_media_group(self, **kwargs: Any) -> list[FakeMessage]:
        self.calls.append(("send_media_group", kwargs))
        media = kwargs.get("media") or []
        return [FakeMessage(self._next_id + index + 1) for index in range(len(media))]

    async def send_video(self, **kwargs: Any) -> FakeMessage:
        return self._record("send_video", kwargs)


# ------------------------------------------------------------------ fixtures
def publication(tweet_id: str = "1") -> Publication:
    return Publication(
        id=uuid.uuid4(),
        tweet_id=tweet_id,
        channel_id=CHANNEL_ID,
        mode=PublicationMode.AUTO,
    )


def downloaded(name: str = "clip.mp4", mime: str = "video/mp4") -> DownloadedMedia:
    return DownloadedMedia(
        path=Path("/tmp/workspace") / name, mime=mime, size_bytes=1234, sha256="0" * 64
    )


def video_tweet(**updates: Any) -> TweetData:
    media = MediaData(
        position=0,
        kind=MediaKind.VIDEO,
        remote_url="https://video.twimg.com/clip.mp4",
        content_type="video/mp4",
        duration_ms=15_000,
    )
    defaults: dict[str, Any] = {
        "tweet_id": "1",
        "text": "A clip worth watching.",
        "author_username": "fixture_dev",
        "author_name": "Fixture Dev",
        "media": (media,),
    }
    return TweetData(**{**defaults, **updates})


def photo_tweet() -> TweetData:
    media = MediaData(
        position=0,
        kind=MediaKind.PHOTO,
        remote_url="https://pbs.twimg.com/media/a.jpg",
    )
    return TweetData(
        tweet_id="2",
        text="A photo.",
        author_name="Fixture Dev",
        author_username="fixture_dev",
        media=(media,),
    )


def with_settings(base: Settings, **updates: object) -> Settings:
    return base.model_copy(update=updates)


async def send(
    settings: Settings,
    bot: FakeBot,
    tweet: TweetData,
    *,
    photos: list[DownloadedMedia] | None = None,
    videos: list[DownloadedMedia] | None = None,
    media_failed: list[str] | None = None,
) -> Any:
    publisher = Publisher(settings, bot)  # type: ignore[arg-type]
    return await publisher._send(
        publication=publication(tweet.tweet_id),
        rendered=render_post(tweet, settings),
        photos=photos or [],
        videos=videos or [],
        media_failed=media_failed or [],
    )


# ------------------------------------------------------------------- the video
async def test_a_video_is_uploaded_as_a_playable_clip(settings: Settings) -> None:
    bot = FakeBot()
    clip = downloaded()

    outcome = await send(settings, bot, video_tweet(), videos=[clip])

    assert bot.methods() == ["send_video"]
    sent = bot.kwargs_for("send_video")[0]
    assert sent["chat_id"] == CHANNEL_ID
    assert isinstance(sent["video"], FSInputFile)
    # The path, not the bytes: a 45 MB video must stream from disk.
    assert sent["video"].path == clip.path
    assert sent["supports_streaming"] is True
    assert sent["caption"] == render_post(video_tweet(), settings).caption

    assert outcome.status is PublicationStatus.PUBLISHED
    assert outcome.media_sent == 1
    assert outcome.skipped_media == 0
    assert len(outcome.message_ids) == 1


async def test_a_video_post_carries_no_author_handle_or_source_link(
    settings: Settings,
) -> None:
    """The constraint on every post, asserted on the caption that ships with video."""
    bot = FakeBot()

    await send(settings, bot, video_tweet(), videos=[downloaded()])

    caption = bot.kwargs_for("send_video")[0]["caption"]
    assert "@fixture_dev" not in caption
    assert "x.com" not in caption
    assert "twitter.com" not in caption
    assert "Fixture Dev" in caption


async def test_the_caption_rides_the_first_media_message_only(settings: Settings) -> None:
    """Telegram ignores a caption on a second message; sending one is a no-op that
    looks like it works."""
    bot = FakeBot()

    await send(
        settings,
        bot,
        photo_tweet(),
        photos=[downloaded("a.jpg", "image/jpeg")],
        videos=[downloaded()],
    )

    assert bot.methods() == ["send_photo", "send_video"]
    assert bot.kwargs_for("send_photo")[0]["caption"]
    assert bot.kwargs_for("send_video")[0]["caption"] is None
    assert bot.kwargs_for("send_video")[0]["parse_mode"] is None


async def test_photos_are_still_sent_as_an_album(settings: Settings) -> None:
    """The video path must not have disturbed the photo path."""
    bot = FakeBot()
    tweet = TweetData(
        tweet_id="3",
        text="Three photos.",
        author_name="Fixture Dev",
        media=tuple(
            MediaData(
                position=index,
                kind=MediaKind.PHOTO,
                remote_url=f"https://pbs.twimg.com/media/{index}.jpg",
            )
            for index in range(3)
        ),
    )

    outcome = await send(
        settings,
        bot,
        tweet,
        photos=[downloaded(f"{index}.jpg", "image/jpeg") for index in range(3)],
    )

    assert bot.methods() == ["send_media_group"]
    group = bot.kwargs_for("send_media_group")[0]["media"]
    assert len(group) == 3
    assert outcome.media_sent == 3
    assert outcome.status is PublicationStatus.PUBLISHED


# ------------------------------------------------------------ omissions/failure
async def test_a_video_without_a_playable_url_is_a_counted_omission(
    settings: Settings,
) -> None:
    """HLS-only and unavailable videos are described but not fetchable. The post
    degrades to text and is reported as partial — never as a silent success."""
    bot = FakeBot()
    unplayable = MediaData(
        position=0,
        kind=MediaKind.VIDEO,
        remote_url="",
        content_type="video/mp4",
    )
    tweet = video_tweet(media=(unplayable,))

    outcome = await send(settings, bot, tweet)

    assert bot.methods() == ["send_message"]
    assert outcome.status is PublicationStatus.PARTIAL
    assert outcome.skipped_media == 1
    assert outcome.media_sent == 0


async def test_video_is_omitted_when_the_setting_is_off(settings: Settings) -> None:
    bot = FakeBot()
    off = with_settings(settings, ALLOW_VIDEO=False)

    outcome = await send(off, bot, video_tweet())

    assert bot.methods() == ["send_message"]
    assert outcome.skipped_media == 1
    assert outcome.status is PublicationStatus.PARTIAL


async def test_a_failed_video_download_downgrades_the_post_to_text(
    settings: Settings,
) -> None:
    bot = FakeBot()

    outcome = await send(
        settings, bot, video_tweet(), media_failed=["position 0: media transfer failed"]
    )

    assert bot.methods() == ["send_message"]
    assert outcome.status is PublicationStatus.PARTIAL
    assert outcome.media_failed == 1
    assert outcome.media_sent == 0


async def test_a_text_only_tweet_is_still_published_plainly(settings: Settings) -> None:
    bot = FakeBot()
    tweet = TweetData(tweet_id="4", text="Just words.", author_name="Fixture Dev")

    outcome = await send(settings, bot, tweet)

    assert bot.methods() == ["send_message"]
    assert outcome.status is PublicationStatus.PUBLISHED
    assert outcome.skipped_media == 0


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (LongCaptionMode.TEXT_FIRST, ["send_message", "send_video"]),
        (LongCaptionMode.MEDIA_FIRST, ["send_video", "send_message"]),
    ],
)
async def test_a_long_video_post_keeps_the_configured_message_order(
    settings: Settings, mode: LongCaptionMode, expected: list[str]
) -> None:
    """A video with text over the caption budget follows LONG_CAPTION_MODE, like
    a photo post does."""
    bot = FakeBot()
    tweet = video_tweet(text="w" * 3000)
    ordered = with_settings(settings, LONG_CAPTION_MODE=mode)

    await send(ordered, bot, tweet, videos=[downloaded()])

    assert bot.methods() == expected
