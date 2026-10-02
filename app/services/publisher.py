"""Publishing a tweet to the channel.

## The order of operations is the design

1. **Pace.** Ask the throttle for a slot. If it says wait, defer the job rather
   than sleeping — a sleeping worker holds one of the few publish slots while
   doing nothing, and `MAX_CONCURRENT_PUBLISHES` defaults to 2.
2. **Render.** Pure CPU, no side effects, so a template error cannot leave a
   half-posted tweet in the channel.
3. **Download everything.** All photos and video, before a single byte reaches
   Telegram. This is what makes `MEDIA_FAILURE_MODE=fail` mean anything: with a
   partial upload already in the channel, "fail" would be a lie.
4. **Send.** Text and/or media, in the order `LONG_CAPTION_MODE` asked for.
5. **Record.** Message IDs, caption, counts and the daily statistic in one
   transaction, so the duplicate-suppression row and the statistic describing it
   can never disagree.

## What a publish can end as

`published` — everything the tweet had was sent. `partial` — something reached
the channel but not all of it (a photo or video failed and was skipped, or the
tweet carried something this build does not publish, such as an animation). `failed` — nothing was sent.

`partial` is never retried automatically. Retrying would double-post the part
that worked, and the duplicate-suppression index would not stop it: the
publication row already exists, and a retry is a different job acting on the same
row. Deleting the post and re-submitting is a deliberate human decision, which is
the right level of ceremony for "this went out wrong".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile, InputMediaPhoto, LinkPreviewOptions
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MediaFailureMode, Settings
from app.constants import CHANNEL_MESSAGES_PER_MINUTE, MAX_MEDIA_GROUP_ITEMS
from app.database.enums import MediaKind, PublicationStatus
from app.database.models import MediaItem, Publication, Tweet
from app.database.repositories.publications import PublicationRepository
from app.database.repositories.stats import StatsRepository, local_day
from app.database.repositories.throttle import ThrottleRepository, channel_scope
from app.logging_config import get_logger
from app.services.errors import JobDeferral
from app.services.fetcher import to_tweet_data
from app.services.media import DownloadedMedia, MediaDownloader, MediaError
from app.services.render import RenderedPost, render_post
from app.utils.files import human_size, temp_workspace
from app.utils.timeutil import utcnow

log = get_logger(__name__)


class PublishError(Exception):
    """A publish that did not happen. `retryable` drives the job's retry decision."""

    def __init__(
        self, message: str, *, retryable: bool = True, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    publication_id: uuid.UUID
    status: PublicationStatus
    message_ids: tuple[int, ...]
    caption: str
    media_sent: int
    media_failed: int
    skipped_media: int
    message_url: str | None

    @property
    def primary_message_id(self) -> int | None:
        return self.message_ids[0] if self.message_ids else None


def _retry_after_seconds(exc: TelegramRetryAfter) -> float:
    """aiogram has exposed `retry_after` as both an int and a timedelta."""
    value = getattr(exc, "retry_after", None)
    if value is None:
        return 5.0
    to_seconds = getattr(value, "total_seconds", None)
    if callable(to_seconds):
        return float(to_seconds())
    try:
        return float(value)
    except (TypeError, ValueError):
        return 5.0


def _is_photo(item: MediaItem) -> bool:
    return _kind(item) is MediaKind.PHOTO


def _is_video(item: MediaItem) -> bool:
    return _kind(item) is MediaKind.VIDEO


def _kind(item: MediaItem) -> MediaKind:
    kind = item.kind
    return kind if isinstance(kind, MediaKind) else MediaKind(kind)


class Publisher:
    """Renders a stored tweet and sends it to the channel."""

    def __init__(self, settings: Settings, bot: Bot) -> None:
        self._settings = settings
        self._bot = bot

    async def publish(
        self,
        session: AsyncSession,
        *,
        publication: Publication,
        tweet_row: Tweet,
        media_items: list[MediaItem],
    ) -> PublishOutcome:
        settings = self._settings
        channel_id = publication.channel_id

        tweet = to_tweet_data(tweet_row, media_items)
        rendered = render_post(tweet, settings)

        await self._claim_publish_slot(session, channel_id)

        with temp_workspace(
            Path(settings.MEDIA_TMP_DIR), prefix="publish-"
        ) as workspace:
            photos, videos, media_failed = await self._download_media(
                media_items, workspace
            )
            outcome = await self._send(
                publication=publication,
                rendered=rendered,
                photos=photos,
                videos=videos,
                media_failed=media_failed,
            )

        await self._record(session, publication, outcome)
        return outcome

    # ----------------------------------------------------------------- pacing

    async def _claim_publish_slot(self, session: AsyncSession, channel_id: int) -> None:
        """Take a throttle slot, or defer the job.

        Deferring rather than sleeping keeps a busy channel from occupying every
        publish slot with workers that are only waiting for the clock.
        """
        wait = await ThrottleRepository(session).acquire(
            channel_scope(channel_id),
            min_interval=self._settings.PUBLISH_COOLDOWN,
            max_per_window=CHANNEL_MESSAGES_PER_MINUTE,
            window_seconds=60.0,
        )
        if wait > 0:
            raise JobDeferral(wait, f"publish cooldown: {wait:.1f}s remaining")

    # ---------------------------------------------------------------- download

    async def _download_media(
        self, media_items: list[MediaItem], workspace: Path
    ) -> tuple[list[DownloadedMedia], list[DownloadedMedia], list[str]]:
        """Download every attachment this build can publish.

        Photos and video are both downloaded here, before a single byte reaches
        Telegram. Media this build does not publish — animation, video when
        `ALLOW_VIDEO=false`, and an item with no usable URL — is skipped without
        being counted as a failure: it is a scope limit, not something that went
        wrong, and the preview already told the admin it would be omitted.
        """
        photos = [item for item in media_items if _is_photo(item) and item.remote_url]
        videos = (
            [item for item in media_items if _is_video(item) and item.remote_url]
            if self._settings.ALLOW_VIDEO
            else []
        )
        if not photos and not videos:
            return [], [], []

        downloaded_photos: list[DownloadedMedia] = []
        downloaded_videos: list[DownloadedMedia] = []
        failures: list[str] = []

        async with MediaDownloader(self._settings) as downloader:
            for item in photos:
                media = await self._download_one(
                    downloader.download_photo, item, workspace, failures
                )
                if media is not None:
                    downloaded_photos.append(media)

            for item in videos:
                media = await self._download_one(
                    downloader.download_video, item, workspace, failures
                )
                if media is not None:
                    downloaded_videos.append(media)

        if failures and self._settings.MEDIA_FAILURE_MODE is MediaFailureMode.FAIL:
            # Nothing has been sent yet — which is exactly why downloads happen
            # before the first Telegram call. Aborting here leaves the channel
            # untouched and the retry can succeed cleanly.
            raise PublishError(
                "media download failed and MEDIA_FAILURE_MODE=fail: " + "; ".join(failures),
                retryable=True,
            )

        return downloaded_photos, downloaded_videos, failures

    async def _download_one(
        self,
        download: Callable[[MediaItem, Path], Awaitable[DownloadedMedia]],
        item: MediaItem,
        workspace: Path,
        failures: list[str],
    ) -> DownloadedMedia | None:
        """One attachment, recorded as a failure rather than raised.

        A single unreachable photo must not abandon the others; the caller
        decides what a failure means from the collected list.
        """
        try:
            media = await download(item, workspace)
        except MediaError as exc:
            failures.append(f"position {item.position}: {exc}")
            log.warning(
                "media download failed",
                extra={
                    "position": item.position,
                    "kind": _kind(item).value,
                    "error": str(exc),
                },
            )
            return None

        log.debug(
            "downloaded media",
            extra={
                "position": item.position,
                "kind": _kind(item).value,
                "size": human_size(media.size_bytes),
            },
        )
        return media

    # -------------------------------------------------------------------- send

    async def _send(
        self,
        *,
        publication: Publication,
        rendered: RenderedPost,
        photos: list[DownloadedMedia],
        videos: list[DownloadedMedia],
        media_failed: list[str],
    ) -> PublishOutcome:
        channel_id = publication.channel_id
        message_ids: list[int] = []

        if not photos and not videos:
            # Either the tweet had no media, or all of it was skipped or failed.
            # The post degrades to text: with the source link gone, `text_link`
            # and `skip_media` both mean "send the text on its own".
            message_ids.append(
                await self._send_text(channel_id, rendered.caption, preview=True)
            )
            degraded = bool(media_failed) or rendered.skipped_media_count > 0
            return PublishOutcome(
                publication_id=publication.id,
                status=(
                    PublicationStatus.PARTIAL if degraded else PublicationStatus.PUBLISHED
                ),
                message_ids=tuple(message_ids),
                caption=rendered.caption,
                media_sent=0,
                media_failed=len(media_failed),
                skipped_media=rendered.skipped_media_count,
                message_url=self._settings.channel_message_url(message_ids[0]),
            )

        if rendered.order == "text_first" and rendered.text_message:
            message_ids.append(
                await self._send_text(channel_id, rendered.text_message, preview=False)
            )

        # The caption belongs on the first media message of whichever kind goes
        # out first — Telegram ignores a caption on a second message, so carrying
        # it forward would be code that silently does nothing.
        caption = rendered.caption
        if photos:
            message_ids.extend(await self._send_photos(channel_id, photos, caption))
            caption = ""
        for video in videos:
            message_ids.append(await self._send_video(channel_id, video, caption))
            caption = ""

        if rendered.order == "media_first" and rendered.text_message:
            message_ids.append(
                await self._send_text(channel_id, rendered.text_message, preview=False)
            )

        # Deliberate partials: content the tweet had that did not reach the
        # channel. A truncated caption is *not* counted — that is
        # LONG_CAPTION_MODE doing what it was configured to do, and flagging it
        # would mark ordinary long posts as problems.
        partial = bool(media_failed) or rendered.skipped_media_count > 0

        return PublishOutcome(
            publication_id=publication.id,
            status=PublicationStatus.PARTIAL if partial else PublicationStatus.PUBLISHED,
            message_ids=tuple(message_ids),
            caption=rendered.caption,
            media_sent=len(photos) + len(videos),
            media_failed=len(media_failed),
            skipped_media=rendered.skipped_media_count,
            message_url=self._settings.channel_message_url(message_ids[0]),
        )

    async def _send_text(self, channel_id: int, markup: str, *, preview: bool) -> int:
        message = await self._call(
            self._bot.send_message(
                chat_id=channel_id,
                text=markup,
                parse_mode="HTML",
                # A preview card under a text-only post is useful; under one that
                # already carries the photos it is redundant clutter.
                link_preview_options=LinkPreviewOptions(is_disabled=not preview),
            )
        )
        return message.message_id

    async def _send_photos(
        self, channel_id: int, photos: list[DownloadedMedia], caption: str
    ) -> list[int]:
        """Send photos as albums, chunked to Telegram's 10-per-group limit.

        The caption rides on the first item only. Telegram ignores captions on
        every other item of an album, so attaching it to each would be code that
        silently does nothing.
        """
        message_ids: list[int] = []
        for start in range(0, len(photos), MAX_MEDIA_GROUP_ITEMS):
            chunk = photos[start : start + MAX_MEDIA_GROUP_ITEMS]
            chunk_caption = caption if start == 0 else ""

            if len(chunk) == 1:
                message = await self._call(
                    self._bot.send_photo(
                        chat_id=channel_id,
                        photo=_input_file(chunk[0]),
                        caption=chunk_caption or None,
                        parse_mode="HTML" if chunk_caption else None,
                    )
                )
                message_ids.append(message.message_id)
                continue

            media = [
                InputMediaPhoto(
                    media=_input_file(item),
                    caption=(chunk_caption if index == 0 and chunk_caption else None),
                    parse_mode=("HTML" if index == 0 and chunk_caption else None),
                )
                for index, item in enumerate(chunk)
            ]
            sent = await self._call(
                self._bot.send_media_group(chat_id=channel_id, media=media)
            )
            message_ids.extend(message.message_id for message in sent)

        return message_ids

    async def _send_video(
        self, channel_id: int, video: DownloadedMedia, caption: str
    ) -> int:
        """Send one video.

        Videos are separate messages rather than a group: a tweet carries at
        most one, and `sendVideo` is what makes Telegram treat the upload as a
        playable clip rather than a document. `supports_streaming` lets it start
        playing before the whole file has arrived.
        """
        message = await self._call(
            self._bot.send_video(
                chat_id=channel_id,
                video=_input_file(video),
                caption=caption or None,
                parse_mode="HTML" if caption else None,
                supports_streaming=True,
            )
        )
        return message.message_id

    async def _call(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run one Telegram call, translating failures into our own exceptions.

        The mapping is the point:

        * `TelegramRetryAfter` becomes a `JobDeferral`, so flood control costs the
          job nothing — it is not a failure, it is a schedule.
        * Forbidden and BadRequest are **not** retryable. Retrying a request
          Telegram has already rejected produces the identical rejection five
          times, more slowly; the operator needs the message now, not after the
          retry budget drains.
        * Network errors are retryable, because they say nothing about the request.
        """
        try:
            return await coroutine
        except TelegramRetryAfter as exc:
            raise JobDeferral(_retry_after_seconds(exc), "Telegram flood control") from exc
        except TelegramForbiddenError as exc:
            raise PublishError(
                "Telegram refused the publish — is the bot an administrator of the "
                f"channel with the 'Post Messages' permission? ({exc})",
                retryable=False,
            ) from exc
        except TelegramBadRequest as exc:
            raise PublishError(
                f"Telegram rejected the message: {exc}", retryable=False
            ) from exc
        except TelegramNetworkError as exc:
            raise PublishError(
                f"network error talking to Telegram: {exc}", retryable=True
            ) from exc
        except TelegramAPIError as exc:
            raise PublishError(f"Telegram API error: {exc}", retryable=True) from exc

    # ------------------------------------------------------------------ record

    async def _record(
        self, session: AsyncSession, publication: Publication, outcome: PublishOutcome
    ) -> None:
        await PublicationRepository(session).mark_published(
            publication,
            message_ids=list(outcome.message_ids),
            primary_message_id=outcome.primary_message_id,
            message_url=outcome.message_url,
            caption=outcome.caption,
            media_sent=outcome.media_sent,
            status=outcome.status,
        )

        # A partial publish counts as published: something did reach the channel,
        # and "how many posts went out today" is the question this table answers.
        # The failure detail lives on the publication row.
        await StatsRepository(session).bump(
            day=local_day(self._settings.tzinfo),
            channel_id=publication.channel_id,
            published=1,
            published_at=utcnow(),
        )

        log.info(
            "published tweet",
            extra={
                "publication_id": str(publication.id),
                "tweet_id": publication.tweet_id,
                "status": outcome.status.value,
                "media_sent": outcome.media_sent,
                "media_failed": outcome.media_failed,
                "skipped_media": outcome.skipped_media,
                "messages": len(outcome.message_ids),
            },
        )


def _input_file(media: DownloadedMedia) -> FSInputFile:
    """Stream from disk rather than reading into memory.

    `BufferedInputFile` would hold every attachment in the heap at once — up to
    500 MB for a ten-photo album, or a single 50 MB video. `FSInputFile` hands
    the path to aiohttp, which streams it.
    """
    return FSInputFile(media.path, filename=media.path.name)
