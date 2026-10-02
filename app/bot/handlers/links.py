"""The URL handler: a tweet link in, a queued fetch out.

Nothing is fetched inline. The handler parses, records a pending submission and
enqueues a job; the worker does the network work and edits the "Fetching…"
placeholder into a receipt before publishing to the channel. A handler that
fetched inline would block the bot's update loop for as long as the provider
takes, and would lose the work entirely if the container restarted mid-request.

**Deduplication happens at three levels, and they are not redundant:**

* `submissions` (partial unique on admin + tweet, pending only) — the same admin
  sending the same link twice edits the existing receipt rather than stacking a
  second one.
* `jobs.dedupe_key` (`fetch:<admin>:<tweet>`) — the same admin, same tweet, still
  in flight. Scoped per admin rather than per tweet so a *second* admin asking
  for the same link still gets their own receipt.
* `publications` (partial unique on tweet + channel) — the one that actually
  matters. It is enforced in the database, so it holds even if two admins submit
  the same link at the same instant.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import messages
from app.bot.context import BotContext
from app.config import Settings
from app.database.enums import JobType
from app.database.repositories.jobs import JobRepository
from app.database.repositories.publications import PublicationRepository
from app.database.repositories.submissions import SubmissionRepository
from app.logging_config import get_logger
from app.utils.text import escape_html
from app.utils.urls import (
    InvalidTweetUrlError,
    ParsedTweetUrl,
    find_tweet_urls,
    looks_like_tweet_link,
    parse_tweet_url,
)

log = get_logger(__name__)

router = Router(name="links")

#: A single message with more links than this is almost certainly a paste
#: accident, and each one costs a provider call and a preview message.
MAX_LINKS_PER_MESSAGE = 5

#: How long a pending submission stays valid before the sweeper expires it.
SUBMISSION_TTL_SECONDS = 3600


@router.message(F.text & ~F.text.startswith("/"))
async def handle_message(
    message: Message,
    ctx: BotContext,
    settings: Settings,
    session: AsyncSession,
) -> None:
    text = message.text or ""
    found = find_tweet_urls(text, limit=MAX_LINKS_PER_MESSAGE + 1)

    if not found:
        await _explain_if_linkish(message, text)
        return

    overflow = len(found) > MAX_LINKS_PER_MESSAGE
    await _queue_links(message, ctx, settings, session, found[:MAX_LINKS_PER_MESSAGE])

    if overflow:
        await message.answer(
            f"Only the first {MAX_LINKS_PER_MESSAGE} links were queued; "
            "send the rest in another message."
        )


async def _queue_links(
    message: Message,
    ctx: BotContext,
    settings: Settings,
    session: AsyncSession,
    parsed_urls: list[ParsedTweetUrl],
) -> None:
    """Record and enqueue every link in one transaction.

    All of them or none: a half-processed paste would leave receipts with no job
    behind them, and the admin would wait forever for a fetch nobody scheduled.
    """
    user = message.from_user
    if user is None:  # pragma: no cover - a message without a sender has no admin
        return

    jobs = JobRepository(session)
    publications = PublicationRepository(session)
    submissions = SubmissionRepository(session)

    for parsed in parsed_urls:
        existing = await publications.find_active(
            tweet_id=parsed.tweet_id, channel_id=ctx.channel_id
        )
        if existing is not None:
            await message.answer(
                messages.ALREADY_PUBLISHED.format(
                    tweet_id=escape_html(parsed.tweet_id),
                    link=escape_html(existing.message_url or "(link unavailable)"),
                ),
                parse_mode="HTML",
            )
            continue

        submission, created = await submissions.get_or_create(
            admin_user_id=user.id,
            chat_id=message.chat.id,
            tweet_id=parsed.tweet_id,
            ttl_seconds=SUBMISSION_TTL_SECONDS,
        )
        if not created and submission.preview_message_id is not None:
            # This admin already has the receipt on screen; a second one would
            # duplicate the message for a single tweet.
            await message.answer(
                messages.ALREADY_QUEUED.format(tweet_id=escape_html(parsed.tweet_id)),
                parse_mode="HTML",
            )
            continue

        status_message = await message.answer(
            messages.FETCHING.format(tweet_id=escape_html(parsed.tweet_id)),
            parse_mode="HTML",
        )

        job, job_created = await jobs.enqueue(
            type=JobType.FETCH_TWEET,
            payload={
                "tweet_id": parsed.tweet_id,
                "submission_id": str(submission.id),
                "chat_id": message.chat.id,
                "status_message_id": status_message.message_id,
                "admin_user_id": user.id,
            },
            dedupe_key=f"fetch:{user.id}:{parsed.tweet_id}",
            requested_by=user.id,
            chat_id=message.chat.id,
            max_attempts=settings.MAX_RETRIES,
        )
        if not job_created:
            log.info(
                "fetch already in flight for this admin",
                extra={"tweet_id": parsed.tweet_id, "user_id": user.id},
            )


async def _explain_if_linkish(message: Message, text: str) -> None:
    """Reply only when the text *looks* like a tweet link but does not parse.

    Replying to every message that mentions an X host would make the bot noisy in
    a staff chat where people discuss links. Confining the feedback to failed
    parses keeps it where it is actually useful.
    """
    if not looks_like_tweet_link(text):
        return

    candidate = next(
        (token for token in text.split() if "x.com" in token or "twitter.com" in token),
        text,
    )
    try:
        parse_tweet_url(candidate)
    except InvalidTweetUrlError as exc:
        await message.answer(messages.describe_invalid_link(exc.reason), parse_mode="HTML")
        return
    await message.answer(messages.NOT_A_TWEET_LINK, parse_mode="HTML")
