"""What a claimed queue row actually does.

Two handlers, one per job type. Both are written to the same rule: **the handler
owns the transaction, and the caller's rollback must not lose the record of what
happened.** That is why a failed publication is marked in a *separate* session —
marking it inside the transaction that is about to roll back would erase the very
row that releases the duplicate-suppression index, and the retry would then be
blocked by a publication claiming to still be pending.

The other rule is that a handler never sleeps and never retries by itself. It
raises, and the runner in `app/worker/runner.py` decides whether that means
"again in N seconds" (a retryable error), "never" (a permanent one), or "not
yet, and it costs nothing" (`JobDeferral`).

Notifications are returned rather than sent. A handler that messaged the admin
mid-transaction could announce a publish that then rolled back; the runner sends
whatever the handler returns only after the commit has landed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import messages
from app.bot.context import BotContext
from app.bot.keyboards import resolved_keyboard
from app.config import Settings
from app.database.enums import (
    JobStatus,
    JobType,
    PublicationMode,
    PublicationStatus,
    SubmissionStatus,
)
from app.database.models import Job
from app.database.repositories.jobs import JobRepository
from app.database.repositories.publications import PublicationRepository
from app.database.repositories.stats import StatsRepository, local_day
from app.database.repositories.submissions import SubmissionRepository
from app.database.repositories.tweets import TweetRepository
from app.database.session import session_scope
from app.logging_config import correlation_context, get_logger
from app.services.errors import JobDeferral, PermanentJobError
from app.services.fetcher import to_tweet_data
from app.services.render import render_preview
from app.utils.text import escape_html, smart_truncate

log = get_logger(__name__)

#: How much of an upstream error to repeat to a human. The full text is in the
#: job row and the logs; a Telegram message is not the place for a stack trace.
_REASON_MAX_CHARS = 300

#: Returned in a handler's result dict to have the runner send a message *after*
#: the transaction commits. Popped before the result is stored, so the JSONB
#: column never sees it.
NOTIFY_KEY = "notify"


class JobRunner:
    """Executes one job, start to finish, and records the outcome."""

    def __init__(self, settings: Settings, bot: Bot, ctx: BotContext) -> None:
        self._settings = settings
        self._bot = bot
        self._ctx = ctx

    # ------------------------------------------------------------------ public

    async def process(self, job_id: uuid.UUID) -> None:
        """Run a claimed job. Never raises: the outcome belongs in the queue."""
        notify: tuple[int, str] | None = None
        try:
            async with session_scope(self._settings) as session:
                job = await session.get(Job, job_id, populate_existing=True)
                if job is None:
                    # Cancelled or deleted between the claim and here. Nothing to
                    # run and nothing to record.
                    log.warning("claimed job no longer exists", extra={"job_id": str(job_id)})
                    return
                with correlation_context(str(job.correlation_id or job.id)):
                    result = await self._dispatch(session, job)
                    notify = result.pop(NOTIFY_KEY, None)
                    await JobRepository(session).mark_succeeded(job, result=result)
                    log.info(
                        "job succeeded",
                        extra={
                            "job_id": str(job.id),
                            "type": _type_of(job).value,
                            "attempts": job.attempts,
                        },
                    )
        except JobDeferral as exc:
            await self._defer(job_id, exc)
        except Exception as exc:  # noqa: BLE001 - every failure becomes job state
            notify = await self._fail(job_id, exc)

        if notify is not None:
            await self._deliver(notify)

    # --------------------------------------------------------------- dispatch

    async def _dispatch(self, session: AsyncSession, job: Job) -> dict[str, Any]:
        job_type = _type_of(job)
        if job_type is JobType.FETCH_TWEET:
            return await self._fetch_tweet(session, job)
        if job_type is JobType.PUBLISH_TWEET:
            return await self._publish_tweet(session, job)
        # `download_media` has no handler in this build: media is downloaded as
        # part of a publish, into a per-publish workspace that is removed when it
        # ends. Nothing enqueues the type, and if a row ever appears it must fail
        # loudly rather than sit pending forever.
        raise PermanentJobError(f"no handler for job type {job_type.value!r}")

    # ------------------------------------------------------------ fetch_tweet

    async def _fetch_tweet(self, session: AsyncSession, job: Job) -> dict[str, Any]:
        """Fetch the post and publish it to the channel.

        There is no confirmation step: the admin sending the link is the
        approval. The status message the handler already sent is edited into an
        informational receipt (what was fetched, what will be omitted), which
        keeps one link = one message in the admin's chat and means a restart
        between the two does not leave an orphaned placeholder behind.
        """
        payload = _payload(job)
        tweet_id = payload.get("tweet_id")
        if not isinstance(tweet_id, str) or not tweet_id:
            raise PermanentJobError(f"job {job.id} has no tweet_id in its payload")

        chat_id = _int_or_none(payload.get("chat_id")) or job.chat_id
        status_message_id = _int_or_none(payload.get("status_message_id"))
        submission_id = _uuid_or_none(payload.get("submission_id"))

        settings = self._settings

        # Raises `TweetNotFoundError` / `TweetUnavailableError` (both
        # non-retryable) or a retryable `ProviderError`. Either way the runner
        # records it; the negative result is cached by the fetcher so a dead link
        # is not re-requested on every attempt.
        tweet_row, fetched = await self._ctx.fetcher.get(session, tweet_id)
        media = await TweetRepository(session).media_for(tweet_id)
        tweet = to_tweet_data(tweet_row, media)

        if fetched:
            # Counted only when the provider was actually called. A cache hit is
            # not a fetch, and counting it as one would make the daily number
            # describe nothing.
            await StatsRepository(session).bump(
                day=local_day(settings.tzinfo),
                channel_id=self._ctx.channel_id,
                fetched=1,
            )

        submissions = SubmissionRepository(session)
        submission = await submissions.get(submission_id) if submission_id else None
        if submission is None or submission.status is not SubmissionStatus.PENDING:
            # Expired by the sweeper, or already handled. Without this check the
            # job would publish and then try to resolve a submission that is gone.
            log.info(
                "fetch finished but the submission is gone",
                extra={"tweet_id": tweet_id, "submission_id": str(submission_id)},
            )
            return {
                "status": "orphaned",
                "tweet_id": tweet_id,
                NOTIFY_KEY: (chat_id, messages.SUBMISSION_GONE) if chat_id else None,
            }

        publications = PublicationRepository(session)
        existing = await publications.find_active(
            tweet_id=tweet_id, channel_id=self._ctx.channel_id
        )

        if existing is not None:
            await self._show(
                submissions,
                submission,
                chat_id,
                status_message_id,
                render_preview(tweet, settings, already_published=True),
                markup=resolved_keyboard(),
            )
            await submissions.resolve(submission, SubmissionStatus.SUPERSEDED)
            return {"status": "already_published", "tweet_id": tweet_id}

        return await self._auto_publish(
            session, job, tweet, tweet_row, submission, chat_id, status_message_id
        )

    async def _auto_publish(
        self,
        session: AsyncSession,
        job: Job,
        tweet: Any,
        tweet_row: Any,
        submission: Any,
        chat_id: int | None,
        status_message_id: int | None,
    ) -> dict[str, Any]:
        """Reserve and queue the publish without waiting for a human.

        The reservation happens here rather than in the fetch so that a failure to
        reserve — the tweet was published by someone else a moment ago — is
        reported to the admin instead of surfacing later as a confusing publish
        error.
        """
        settings = self._settings
        publications = PublicationRepository(session)
        submissions = SubmissionRepository(session)

        publication = await publications.reserve(
            tweet_id=tweet.tweet_id,
            channel_id=self._ctx.channel_id,
            mode=PublicationMode.AUTO,
            template=settings.POST_TEMPLATE.value,
            media_count=tweet_row.media_count,
            published_by=job.requested_by,
        )
        if publication is None:
            await self._show(
                submissions,
                submission,
                chat_id,
                status_message_id,
                render_preview(tweet, settings, already_published=True),
                markup=resolved_keyboard(),
            )
            await submissions.resolve(submission, SubmissionStatus.SUPERSEDED)
            return {"status": "already_published", "tweet_id": tweet.tweet_id}

        publish_job, created = await JobRepository(session).enqueue(
            type=JobType.PUBLISH_TWEET,
            payload={
                "tweet_id": tweet.tweet_id,
                "publication_id": str(publication.id),
                "chat_id": chat_id,
                "notify_chat_id": chat_id,
                "admin_user_id": job.requested_by,
                "automatic": True,
            },
            dedupe_key=f"publish:{tweet.tweet_id}:{self._ctx.channel_id}",
            requested_by=job.requested_by,
            chat_id=chat_id,
            max_attempts=settings.MAX_RETRIES,
        )
        if not created:
            # Our reservation would otherwise sit `pending` forever, blocking
            # every future attempt at this tweet. Release it explicitly.
            await publications.mark_failed(publication, "a publish job was already queued")
            await submissions.resolve(submission, SubmissionStatus.SUPERSEDED)
            return {"status": "already_queued", "tweet_id": tweet.tweet_id}

        await self._show(
            submissions,
            submission,
            chat_id,
            status_message_id,
            render_preview(tweet, settings),
            markup=resolved_keyboard(),
        )
        await submissions.resolve(
            submission,
            SubmissionStatus.CONFIRMED,
            job_id=publish_job.id if publish_job else None,
        )
        return {
            "status": "auto_queued",
            "tweet_id": tweet.tweet_id,
            "publication_id": str(publication.id),
        }

    # ---------------------------------------------------------- publish_tweet

    async def _publish_tweet(self, session: AsyncSession, job: Job) -> dict[str, Any]:
        payload = _payload(job)
        publication_id = _uuid_or_none(payload.get("publication_id"))
        if publication_id is None:
            raise PermanentJobError(f"job {job.id} has no publication_id in its payload")

        publications = PublicationRepository(session)
        publication = await publications.get(publication_id)
        if publication is None:
            raise PermanentJobError(f"publication {publication_id} does not exist")
        if publication.status is not PublicationStatus.PENDING:
            # Already published, already failed, or deleted by hand. Publishing
            # again is exactly what the duplicate-suppression index exists to
            # prevent, so this is a success with nothing to do.
            return {
                "status": "skipped",
                "tweet_id": publication.tweet_id,
                "publication_status": publication.status.value,
            }

        tweets = TweetRepository(session)
        tweet_row = await tweets.get(publication.tweet_id)
        if tweet_row is None or tweet_row.unavailable_reason is not None:
            await publications.mark_failed(
                publication, "the post is no longer available from the provider"
            )
            return {"status": "unavailable", "tweet_id": publication.tweet_id}

        media = await tweets.media_for(publication.tweet_id)

        try:
            outcome = await self._ctx.publisher.publish(
                session,
                publication=publication,
                tweet_row=tweet_row,
                media_items=media,
            )
        except JobDeferral:
            # The throttle said "not now". The reservation stays `pending` so the
            # duplicate guard keeps holding while the job waits its turn.
            raise
        except Exception as exc:
            await self._fail_publication(publication_id, exc)
            raise

        notify_chat = _int_or_none(payload.get("notify_chat_id")) or _int_or_none(
            payload.get("chat_id")
        )
        notice = _publish_notice(outcome)
        return {
            "status": outcome.status.value,
            "tweet_id": publication.tweet_id,
            "publication_id": str(publication.id),
            "message_ids": list(outcome.message_ids),
            "media_sent": outcome.media_sent,
            "media_failed": outcome.media_failed,
            NOTIFY_KEY: (notify_chat, notice) if notify_chat and notice else None,
        }

    async def _fail_publication(self, publication_id: uuid.UUID, exc: Exception) -> None:
        """Release the reservation, in its own transaction.

        This has to be a *second* session. The publish raised, so the surrounding
        transaction is about to roll back — and a `mark_failed` written inside it
        would roll back with it, leaving the row `pending` and the duplicate
        index permanently blocking every future attempt at this tweet.
        """
        detail = f"{type(exc).__name__}: {exc}"
        try:
            async with session_scope(self._settings) as session:
                publications = PublicationRepository(session)
                publication = await publications.get(publication_id)
                if publication is not None and publication.status is PublicationStatus.PENDING:
                    await publications.mark_failed(publication, detail)
        except Exception:  # noqa: BLE001 - never mask the publish error we are reporting
            log.exception(
                "could not mark the publication as failed",
                extra={"publication_id": str(publication_id)},
            )

    # -------------------------------------------------------------- messaging

    async def _show(
        self,
        submissions: SubmissionRepository,
        submission: Any,
        chat_id: int | None,
        status_message_id: int | None,
        text: str,
        *,
        markup: Any = None,
    ) -> int | None:
        """Put the preview on screen, replacing the placeholder if it is still there."""
        message_id = await self._replace_status(chat_id, status_message_id, text, markup=markup)
        if message_id is not None:
            await submissions.set_preview_message(submission, message_id)
        return message_id

    async def _replace_status(
        self,
        chat_id: int | None,
        status_message_id: int | None,
        text: str,
        *,
        markup: Any = None,
    ) -> int | None:
        """Edit the placeholder, falling back to a new message.

        Three outcomes, all normal: the edit lands; Telegram says "message is not
        modified" (already correct — treat as success); or the message is gone,
        too old to edit, or in a chat we can no longer write to, in which case a
        fresh message is sent so the admin still gets their preview.
        """
        if chat_id is None:
            return None

        if status_message_id is not None:
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                return status_message_id
            except TelegramBadRequest as exc:
                if "not modified" in str(exc).lower():
                    return status_message_id
                log.debug(
                    "could not edit the placeholder; sending a new message",
                    extra={"message_id": status_message_id, "error": str(exc)},
                )
            except TelegramAPIError as exc:
                log.debug(
                    "could not edit the placeholder; sending a new message",
                    extra={"message_id": status_message_id, "error": str(exc)},
                )

        try:
            sent = await self._bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup
            )
        except Exception:
            # Deliberately not swallowed. If the admin cannot be shown the
            # preview, the job has not done its job — and letting it fail means
            # the retry is free (the tweet is already cached) and a permanently
            # unreachable chat ends up visible in the queue rather than silent.
            log.warning(
                "could not deliver the preview to the admin",
                extra={"chat_id": chat_id},
            )
            raise
        return sent.message_id

    async def _deliver(self, notify: tuple[int, str] | None) -> None:
        """Send a post-commit notice.

        Catches everything, unlike `_replace_status`: this runs *after* the job's
        outcome has been committed, so an exception here would be recorded
        against a job that already succeeded. The worst case is a missing
        courtesy message, which the log covers.
        """
        if notify is None:
            return
        chat_id, text = notify
        try:
            await self._bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001 - must never affect job state
            log.warning(
                "could not deliver a notification",
                extra={"chat_id": chat_id, "error": str(exc)},
            )

    # ------------------------------------------------------------- job states

    async def _defer(self, job_id: uuid.UUID, exc: JobDeferral) -> None:
        async with session_scope(self._settings) as session:
            job = await session.get(Job, job_id, populate_existing=True)
            if job is None:
                return
            await JobRepository(session).defer(job, seconds=exc.seconds, reason=exc.reason)
            log.info(
                "job deferred",
                extra={
                    "job_id": str(job_id),
                    "type": _type_of(job).value,
                    "seconds": round(exc.seconds, 1),
                    "reason": exc.reason,
                },
            )

    async def _fail(self, job_id: uuid.UUID, exc: Exception) -> tuple[int, str] | None:
        """Record a failure and decide whether the admin needs to hear about it."""
        retryable = bool(getattr(exc, "retryable", True))
        retry_after = getattr(exc, "retry_after", None)
        detail = f"{type(exc).__name__}: {exc}"

        notify: tuple[int, str] | None = None
        async with session_scope(self._settings) as session:
            job = await session.get(Job, job_id, populate_existing=True)
            if job is None:
                return None
            status = await JobRepository(session).mark_failed(
                job,
                detail,
                retryable=retryable,
                base_delay=self._settings.RETRY_BASE_DELAY,
                max_delay=self._settings.RETRY_MAX_DELAY,
                override_delay=(
                    float(retry_after) if isinstance(retry_after, (int, float)) else None
                ),
            )
            notify = _failure_notice(job, status, detail)

        log.warning(
            "job failed",
            extra={
                "job_id": str(job_id),
                "status": status.value,
                "retryable": retryable,
                "error": detail,
            },
        )
        return notify


# --------------------------------------------------------------------- helpers
def _type_of(job: Job) -> JobType:
    """Coerce the column to the enum.

    SQLAlchemy normally hands back the member, but `JobType` is a `StrEnum`, so
    this is correct whether the value came back as a member or a bare string —
    and a `Job` built in a test without a round-trip through the database will
    hand back whatever the test passed.
    """
    return job.type if isinstance(job.type, JobType) else JobType(job.type)


def _payload(job: Job) -> Mapping[str, Any]:
    return job.payload if isinstance(job.payload, Mapping) else {}


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int; it is never a chat id
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _publish_notice(outcome: Any) -> str | None:
    """The admin-facing summary of a completed publish."""
    link = escape_html(outcome.message_url or "")
    if outcome.status is PublicationStatus.PARTIAL:
        bits: list[str] = []
        if outcome.media_failed:
            bits.append(
                f"{outcome.media_failed} media file(s) could not be downloaded and were skipped"
            )
        if outcome.skipped_media:
            bits.append(
                f"{outcome.skipped_media} item(s) this build does not publish were omitted"
            )
        return messages.PUBLISH_PARTIAL.format(
            link=link, detail=escape_html("; ".join(bits) or "see the logs")
        )
    if outcome.status is PublicationStatus.PUBLISHED:
        return messages.PUBLISH_SUCCEEDED.format(link=link)
    return None


def _failure_notice(job: Job, status: JobStatus, detail: str) -> tuple[int, str] | None:
    """What to tell the requesting admin, if anything.

    At most two messages per job: one when it first fails and will be retried,
    one if it reaches a terminal state. Notifying on every attempt would turn a
    flaky provider into a chat full of identical warnings.
    """
    chat_id = job.chat_id
    if chat_id is None:
        return None

    reason = escape_html(smart_truncate(detail, _REASON_MAX_CHARS))
    job_type = _type_of(job)
    tweet_id = _payload(job).get("tweet_id")
    subject = escape_html(tweet_id) if isinstance(tweet_id, str) else "the post"

    if status in (JobStatus.FAILED, JobStatus.DEAD):
        if job_type is JobType.PUBLISH_TWEET:
            return chat_id, messages.PUBLISH_FAILED.format(reason=reason)
        return chat_id, messages.FETCH_FAILED.format(tweet_id=subject, reason=reason)

    if job.attempts <= 1:
        if job_type is JobType.PUBLISH_TWEET:
            return chat_id, messages.PUBLISH_DEFERRED.format(reason=reason)
        return chat_id, messages.FETCH_DEFERRED.format(tweet_id=subject)

    return None
