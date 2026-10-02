"""Publish / Cancel buttons.

The buttons are the only place a human decides whether something goes public, so
the checks here are deliberately paranoid about *identity* (is this the admin the
preview was made for?) and about *state* (is this submission still pending?).
Both are cheap, and both are the difference between a stale button doing nothing
and a stale button publishing something twice.

Everything else is deferred to the worker. The callback reserves the publication
row — which is what actually claims the right to post — enqueues the job, and
returns. It does no network I/O, so the button feels instant and a slow publish
cannot time out Telegram's callback window.
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import messages
from app.bot.context import BotContext
from app.bot.keyboards import PreviewAction, resolved_keyboard
from app.config import Settings
from app.database.enums import (
    JobType,
    PublicationMode,
    SubmissionStatus,
)
from app.database.repositories.jobs import JobRepository
from app.database.repositories.publications import PublicationRepository
from app.database.repositories.submissions import SubmissionRepository
from app.database.repositories.tweets import TweetRepository
from app.logging_config import get_logger
from app.utils.urls import canonical_tweet_url

log = get_logger(__name__)

router = Router(name="callbacks")


@router.callback_query(PreviewAction.filter(F.action == "publish"))
async def handle_publish(
    callback: CallbackQuery,
    callback_data: PreviewAction,
    ctx: BotContext,
    settings: Settings,
    session: AsyncSession,
) -> None:
    submission = await _load_submission(callback, callback_data, session)
    if submission is None:
        return

    tweet_id = submission.tweet_id
    source_url = canonical_tweet_url(tweet_id, None)

    tweets = TweetRepository(session)
    tweet = await tweets.get(tweet_id)
    if tweet is None or tweet.unavailable_reason is not None:
        # The fetch job should have produced a row before the preview existed, so
        # this is either a race or a row pruned underneath us. Either way there is
        # nothing to publish.
        await _finish(callback, source_url, "This post is no longer available.")
        return

    publications = PublicationRepository(session)
    if await publications.find_active(tweet_id=tweet_id, channel_id=ctx.channel_id):
        await _finish(callback, source_url, "Already published to the channel.")
        return

    publication = await publications.reserve(
        tweet_id=tweet_id,
        channel_id=ctx.channel_id,
        mode=PublicationMode.MANUAL,
        template=settings.POST_TEMPLATE.value,
        media_count=tweet.media_count,
        published_by=callback.from_user.id,
    )
    if publication is None:
        # The unique index rejected it — another publish for this tweet and
        # channel is active. Nothing was reserved, so nothing to clean up.
        await _finish(callback, source_url, "Already published to the channel.")
        return

    jobs = JobRepository(session)
    job, created = await jobs.enqueue(
        type=JobType.PUBLISH_TWEET,
        payload={
            "tweet_id": tweet_id,
            "publication_id": str(publication.id),
            "chat_id": submission.chat_id,
            "notify_chat_id": submission.chat_id,
            "admin_user_id": callback.from_user.id,
        },
        dedupe_key=f"publish:{tweet_id}:{ctx.channel_id}",
        requested_by=callback.from_user.id,
        chat_id=submission.chat_id,
        max_attempts=settings.MAX_RETRIES,
    )

    if not created:
        # A publish job for this tweet is already queued. Our reservation would
        # otherwise sit in `pending` forever and block every future attempt, so it
        # is released explicitly.
        await publications.mark_failed(publication, "a publish job was already queued")
        await _finish(callback, source_url, "Already being published.")
        return

    await SubmissionRepository(session).resolve(
        submission, SubmissionStatus.CONFIRMED, job_id=job.id if job else None
    )
    await _finish(callback, source_url, "Queued for publishing.")


@router.callback_query(PreviewAction.filter(F.action == "cancel"))
async def handle_cancel(
    callback: CallbackQuery,
    callback_data: PreviewAction,
    session: AsyncSession,
) -> None:
    submission = await _load_submission(callback, callback_data, session)
    if submission is None:
        return

    await SubmissionRepository(session).resolve(submission, SubmissionStatus.REJECTED)
    await _finish(
        callback,
        canonical_tweet_url(submission.tweet_id, None),
        "Cancelled — nothing was published.",
    )


# --------------------------------------------------------------------- helpers
async def _load_submission(
    callback: CallbackQuery, data: PreviewAction, session: AsyncSession
):
    """Load the submission, or explain why the button did nothing.

    Returns `None` after answering the callback in every failure case, so the
    callers below can assume a live, owned, pending submission.
    """
    try:
        submission_id = uuid.UUID(data.submission_id)
    except ValueError:
        await callback.answer(messages.SUBMISSION_GONE, show_alert=True)
        return None

    submission = await SubmissionRepository(session).get(submission_id)
    if submission is None or submission.status is not SubmissionStatus.PENDING:
        # Expired, already published, or already cancelled. The buttons are
        # stripped so the same stale button cannot be pressed twice.
        await callback.answer(messages.SUBMISSION_GONE, show_alert=True)
        await _strip_buttons(callback, None)
        return None

    if callback.from_user is None or submission.admin_user_id != callback.from_user.id:
        # A preview from a staff chat is visible to every admin in it, but only
        # the admin who submitted the link may act on it.
        log.warning(
            "a different admin tried to act on a submission",
            extra={
                "submission_id": str(submission.id),
                "expected": submission.admin_user_id,
                "actual": callback.from_user.id if callback.from_user else None,
            },
        )
        await callback.answer(messages.SUBMISSION_NOT_YOURS, show_alert=True)
        return None

    return submission


async def _finish(callback: CallbackQuery, source_url: str | None, note: str) -> None:
    """Strip the buttons and tell the admin what happened, in one gesture."""
    await _strip_buttons(callback, source_url)
    await callback.answer(note)


async def _strip_buttons(callback: CallbackQuery, source_url: str | None) -> None:
    message = callback.message
    if message is None:
        return  # an inaccessible message; the callback answer still gets through
    try:
        await message.edit_reply_markup(reply_markup=resolved_keyboard(source_url=source_url))
    except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
        log.debug("could not strip the preview buttons", extra={"error": str(exc)})
