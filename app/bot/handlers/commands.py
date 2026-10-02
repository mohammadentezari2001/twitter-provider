"""Command handlers: /start, /help, /status, /stats, /recent, /id."""

from __future__ import annotations

from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import messages
from app.bot.context import BotContext
from app.config import Settings
from app.constants import CHANNEL_MESSAGES_PER_MINUTE
from app.database.enums import PublicationStatus
from app.database.repositories.jobs import JobRepository
from app.database.repositories.publications import PublicationRepository
from app.database.repositories.stats import StatsRepository, local_day
from app.database.repositories.throttle import ThrottleRepository, channel_scope
from app.database.session import check_database_connection
from app.logging_config import get_logger
from app.utils.text import escape_html
from app.utils.timeutil import humanize_ago, humanize_duration

log = get_logger(__name__)

router = Router(name="commands")

_STATUS_ICON = {
    PublicationStatus.PUBLISHED: "✅",
    PublicationStatus.PARTIAL: "⚠️",
    PublicationStatus.PENDING: "⏳",
    PublicationStatus.FAILED: "❌",
    PublicationStatus.DELETED: "🗑",
}


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(messages.START, parse_mode="HTML")


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(messages.HELP, parse_mode="HTML")


@router.message(Command("id"))
async def handle_id(message: Message) -> None:
    """Report the sender's own ID — the one fact needed to get on the admin list.

    Handled before the admin gate would matter, but the gate runs first anyway;
    the value here is that an admin can add a colleague without opening a
    separate bot to look the ID up.
    """
    user = message.from_user
    if user is None:  # pragma: no cover - channel posts have no from_user
        return
    await message.answer(
        messages.MY_ID.format(user_id=user.id), parse_mode="HTML"
    )


@router.message(Command("status"))
async def handle_status(
    message: Message, ctx: BotContext, settings: Settings, session: AsyncSession
) -> None:
    jobs = JobRepository(session)
    depth = await jobs.queue_depth()
    oldest = await jobs.oldest_pending_age_seconds()

    health = await check_database_connection(settings=settings)

    # The bot process only knows about a worker it is running itself. In the
    # split deployment (`RUN_MODE=bot` + a separate worker container) it has no
    # way to see the other process, so it says so rather than guessing — the
    # oldest-pending-job age below is the cross-process signal.
    if settings.runs_worker and ctx.worker_seen_at is not None:
        worker = messages.WORKER_ALIVE.format(ago=humanize_ago(ctx.worker_seen_at))
    elif settings.runs_worker:
        worker = "starting…"
    else:
        worker = "separate container (see queue below)"

    throttle_wait = await ThrottleRepository(session).wait_time(
        channel_scope(ctx.channel_id),
        min_interval=settings.PUBLISH_COOLDOWN,
        max_per_window=CHANNEL_MESSAGES_PER_MINUTE,
        window_seconds=60.0,
    )

    queue_bits = (
        f"{depth.pending} pending · {depth.running} running · "
        f"{depth.failed} failed · {depth.dead} dead"
    )
    if throttle_wait > 0:
        queue_bits += f" · cooldown {throttle_wait:.1f}s"

    warnings = [
        escape_html(w) for w in settings.startup_warnings() + messages.startup_warnings(settings)
    ]
    warning_block = (
        f"{messages.WARNINGS_HEADER}\n" + "\n".join(f"• {w}" for w in warnings)
        if warnings
        else ""
    )

    await message.answer(
        messages.STATUS.format(
            run_mode=settings.RUN_MODE.value,
            uptime=humanize_duration(ctx.uptime_seconds),
            provider=escape_html(ctx.provider.name),
            channel=escape_html(str(ctx.channel_id)),
            database=(
                f"ok ({health.latency_ms}ms)"
                if health.ok
                else f"❌ {escape_html(health.error or 'unreachable')}"
            ),
            worker=escape_html(worker),
            queue=escape_html(queue_bits),
            oldest=humanize_duration(oldest) if oldest is not None else "—",
            warnings=warning_block,
        ),
        parse_mode="HTML",
    )


@router.message(Command("stats"))
async def handle_stats(
    message: Message, ctx: BotContext, settings: Settings, session: AsyncSession
) -> None:
    stats = StatsRepository(session)
    today_day = local_day(settings.tzinfo)
    today = await stats.for_day(day=today_day, channel_id=ctx.channel_id)
    week = await stats.totals(
        since=today_day - timedelta(days=6),
        until=today_day,
        channel_id=ctx.channel_id,
    )

    await message.answer(
        messages.STATS.format(
            tz=escape_html(settings.TIMEZONE),
            today_published=today.published_count if today else 0,
            today_fetched=today.fetched_count if today else 0,
            today_failed=today.failed_count if today else 0,
            days=7,
            week_published=week["published"],
            week_fetched=week["fetched"],
            week_failed=week["failed"],
            week_skipped=week["skipped"],
        ),
        parse_mode="HTML",
    )


@router.message(Command("recent"))
async def handle_recent(
    message: Message, ctx: BotContext, session: AsyncSession
) -> None:
    publications = await PublicationRepository(session).recent(
        limit=10, channel_id=ctx.channel_id
    )
    if not publications:
        await message.answer(messages.RECENT_EMPTY, parse_mode="HTML")
        return

    rows = []
    for publication in publications:
        icon = _STATUS_ICON.get(publication.status, "•")
        rows.append(
            messages.RECENT_ROW.format(
                icon=icon,
                tweet_id=escape_html(publication.tweet_id),
                status=escape_html(publication.status.value),
                when=escape_html(humanize_ago(publication.published_at or publication.created_at)),
            )
        )
    await message.answer(
        f"{messages.RECENT_HEADER}\n\n" + "\n".join(rows), parse_mode="HTML"
    )
