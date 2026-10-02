"""Application entrypoint. `RUN_MODE` decides which halves of it start.

    bot     — the Telegram dispatcher only.
    worker  — the job loop only.
    all     — both, in one event loop (the default, and what a single-container
              deployment uses).

`docker-compose.yml` runs `bot` and `worker` as separate containers over one
image, so a crash-looping dispatcher cannot stop the queue from draining. Nothing
here depends on that split: the queue is claimed with `FOR UPDATE SKIP LOCKED`,
so both shapes work identically.

## Startup is a sequence, and the order is deliberate

1. **Settings**, then logging, so every later failure is already formatted.
2. **`startup_problems()`** — the blocking list. A missing bot token or a missing
   provider credential stops the process here, with a message naming the variable,
   rather than surfacing as a mysterious failure at the first publish. Exit code 2
   distinguishes "misconfigured" from "crashed" in `docker compose ps`.
3. **Write directories**, so a bad volume mount fails now rather than mid-publish.
4. **Admin roster sync**, which is the first thing that needs the database and
   therefore the first honest test that the database is reachable and migrated.
5. **Channel resolution**, which is the first thing that needs Telegram.
6. Then the long-running parts.

## Shutdown order is the mirror image, and it matters

The worker drains **before** polling stops. A job that is mid-publish needs the
open Telegram session to send its photos and its notification; stopping the
dispatcher first would be fine for that (polling and sending are independent),
but closing the bot's session first would not. So: worker, then polling, then the
heartbeat, then the network handles.

A job that cannot finish inside the grace period is cancelled and left `running`;
`reclaim_expired` returns it to the queue on the next worker's first maintenance
pass. There is no separate "shutdown bookkeeping" path, deliberately: one recovery
mechanism, exercised on every `docker kill`, is worth more than two of which only
one is ever tested.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot import messages
from app.bot.channel import inspect_channel, resolve_channel_id
from app.bot.context import BotContext
from app.bot.dispatcher import build_dispatcher, register_commands
from app.config import ConfigurationError, Settings, load_settings_or_report
from app.database.repositories.admins import AdminRepository
from app.database.session import dispose_engine, session_scope
from app.logging_config import configure_logging, get_logger
from app.providers import build_provider
from app.providers.base import TweetProvider
from app.services.fetcher import TweetFetcher
from app.services.heartbeat import Heartbeat, interval_for
from app.services.publisher import Publisher
from app.utils.files import ensure_dir, prune_stale_dirs, stale_workspace_age
from app.worker import JobRunner, Worker

log = get_logger(__name__)

#: `docker compose ps` shows these, so "the config is wrong" and "it crashed"
#: are distinguishable without opening the logs.
EXIT_CONFIG_ERROR = 2
EXIT_RUNTIME_ERROR = 1

#: How long the worker may spend finishing in-flight jobs at shutdown. Slightly
#: under Compose's `stop_grace_period` (30s) so the container exits by itself
#: rather than being SIGKILLed mid-write.
_WORKER_DRAIN_TIMEOUT = 25.0

#: How long `start_polling` may take to notice it was asked to stop.
_POLLING_STOP_TIMEOUT = 10.0


class StartupError(RuntimeError):
    """A fatal problem discovered while bringing the application up."""


# --------------------------------------------------------------------- startup
def main() -> int:
    """Synchronous entrypoint: `python -m app.main`."""
    try:
        settings = load_settings_or_report()
    except ConfigurationError as exc:
        # Raised before logging exists, and its message never echoes field
        # values, so stderr is both safe and the only place it can go.
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    configure_logging(
        level=settings.LOG_LEVEL,
        fmt=settings.LOG_FORMAT,
        log_dir=settings.LOG_DIR,
        max_bytes=settings.LOG_MAX_BYTES,
        backup_count=settings.LOG_BACKUP_COUNT,
    )

    problems = settings.startup_problems()
    if problems:
        log.critical(
            "configuration is incomplete; refusing to start",
            extra={"problems": problems, "count": len(problems)},
        )
        for problem in problems:
            log.critical("  • %s", problem)
        log.critical("Fix these in .env and restart. See docs/configuration.md.")
        return EXIT_CONFIG_ERROR

    _announce(settings)

    try:
        return asyncio.run(_amain(settings))
    except StartupError as exc:
        log.critical("startup failed: %s", exc)
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:  # pragma: no cover - a second Ctrl-C during teardown
        return 0


def _announce(settings: Settings) -> None:
    """Log what was configured, once, without ever echoing a secret."""
    for warning in settings.startup_warnings() + messages.startup_warnings(settings):
        log.warning(warning)

    log.info(
        "starting tweet-relay-bot",
        extra={
            "run_mode": settings.RUN_MODE.value,
            "provider": settings.TWEET_PROVIDER.value,
            "channel": settings.TELEGRAM_CHANNEL_ID,
            "timezone": settings.TIMEZONE,
            "admins": len(settings.ADMIN_USER_IDS),
            "redis": settings.redis_enabled,
        },
    )
    # The full snapshot is only useful when something is subtly wrong, which is
    # exactly when DEBUG is turned on. Passwords and tokens are already replaced
    # by `safe_dump`.
    log.debug("configuration snapshot", extra={"config": settings.safe_dump()})


# ------------------------------------------------------------------------ main
async def _amain(settings: Settings) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    _prepare_directories(settings)

    heartbeat = Heartbeat(settings.HEARTBEAT_FILE)
    # Beat once before anything slow: the healthcheck's `start_period` then covers
    # the real startup work rather than waiting for the first scheduled beat.
    heartbeat.touch()

    await _sync_admins(settings)
    await _prune_workspaces(settings)

    provider = build_provider(settings)
    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    heartbeat_task = asyncio.create_task(
        heartbeat.run(interval_for(settings.HEARTBEAT_MAX_AGE), stop), name="heartbeat"
    )
    dispatcher: Dispatcher | None = None
    polling_task: asyncio.Task[None] | None = None
    worker_task: asyncio.Task[None] | None = None

    try:
        channel_id = await resolve_channel_id(bot, settings)
        await _report_channel(bot, channel_id, settings)

        ctx = BotContext(
            settings=settings,
            provider=provider,
            fetcher=TweetFetcher(settings, provider),
            publisher=Publisher(settings, bot),
            channel_id=channel_id,
        )

        if settings.runs_bot:
            dispatcher = build_dispatcher(settings, ctx)
            # Cosmetic and non-fatal, but only meaningful for the process that
            # actually receives updates.
            await register_commands(bot)
            polling_task = asyncio.create_task(
                dispatcher.start_polling(bot, handle_signals=False), name="polling"
            )
            log.info("Telegram polling started")

        if settings.runs_worker:
            worker = Worker(settings, JobRunner(settings, bot, ctx), heartbeat)
            worker_task = asyncio.create_task(worker.run(stop), name="worker")

        await stop.wait()
        log.info("shutdown signal received; draining")
    finally:
        # Also reached when startup itself failed, so a half-built process does
        # not leave a polling task or a provider session behind.
        stop.set()
        await _shutdown(
            bot=bot,
            provider=provider,
            dispatcher=dispatcher,
            polling_task=polling_task,
            worker_task=worker_task,
            heartbeat_task=heartbeat_task,
        )

    return 0


# -------------------------------------------------------------------- lifecycle
def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Turn SIGINT/SIGTERM into a graceful stop.

    `add_signal_handler` is used rather than `signal.signal` because it is
    loop-aware: a plain handler runs between bytecodes and cannot touch asyncio
    primitives safely. The fallback exists for platforms without it (notably
    Windows, where the app is never deployed but is occasionally run).
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform
            signal.signal(sig, lambda *_: stop.set())


def _prepare_directories(settings: Settings) -> None:
    """Create the write targets, failing now rather than at the first publish."""
    for path in (settings.MEDIA_TMP_DIR, settings.HEARTBEAT_FILE.parent):
        try:
            ensure_dir(path)
        except OSError as exc:
            raise StartupError(
                f"cannot create {path}: {exc}. Check that the volume is mounted "
                "and writable by uid 10001."
            ) from exc


async def _sync_admins(settings: Settings) -> None:
    """Reconcile the admin roster with `ADMIN_USER_IDS`.

    This is the first database access, so it doubles as the startup check that
    PostgreSQL is reachable *and* migrated — a missing table fails here with a
    clear message instead of much later, inside a handler.
    """
    try:
        async with session_scope(settings) as session:
            activated, deactivated = await AdminRepository(session).sync_from_settings(settings)
    except Exception as exc:
        raise StartupError(
            f"cannot reach the database ({type(exc).__name__}: {exc}). "
            "Check DATABASE_URL and that `alembic upgrade head` has run "
            "(docker compose logs migrate)."
        ) from exc

    log.info("admin roster synchronised", extra={"active": len(activated)})
    if deactivated:
        log.warning(
            "admins deactivated because they are no longer in ADMIN_USER_IDS",
            extra={"user_ids": deactivated},
        )


async def _prune_workspaces(settings: Settings) -> None:
    """Clear media scratch directories left behind by a previous crash."""
    try:
        removed = prune_stale_dirs(
            settings.MEDIA_TMP_DIR,
            max_age_seconds=stale_workspace_age(settings.JOB_LOCK_TIMEOUT),
        )
    except OSError as exc:  # pragma: no cover - unwritable volume
        log.warning("could not prune stale media workspaces", extra={"error": str(exc)})
        return
    if removed:
        log.info("removed stale media workspaces", extra={"count": removed})


async def _report_channel(bot: Bot, channel_id: int, settings: Settings) -> None:
    """Log where posts will go, and whether the bot may actually post there."""
    info = await inspect_channel(bot, channel_id)
    log.info(
        "channel resolved",
        extra={
            "channel_id": info.channel_id,
            "title": info.title,
            "username": info.username,
            "can_post": info.can_post,
        },
    )
    if not info.can_post:
        # A warning, never fatal: the fix is in Telegram's UI, and stopping the
        # process would only hide the rest of the startup diagnostics.
        log.warning(
            messages.PUBLISH_PERMISSION_NOTICE.format(
                channel=info.title, detail=info.permission_detail or "unknown reason"
            )
        )
    if settings.TELEGRAM_CHANNEL_USERNAME or settings.TELEGRAM_CHANNEL_ID.startswith("@"):
        return
    # A private channel has no public username, so the footer of published posts
    # carries only the configured display name and `/recent` falls back to /c/ links.
    log.debug("channel has no username; the footer will omit the @handle")


async def _shutdown(
    *,
    bot: Bot,
    provider: TweetProvider,
    dispatcher: Dispatcher | None,
    polling_task: asyncio.Task[None] | None,
    worker_task: asyncio.Task[None] | None,
    heartbeat_task: asyncio.Task[None],
) -> None:
    """Tear down in the order that keeps in-flight work able to finish."""
    if worker_task is not None:
        # The worker has already seen `stop` and is draining; this is a wait, not
        # a signal. Timing out means a job is stuck, and cancelling is the right
        # answer — it stays `running` and the reclaim sweep picks it up.
        await _finish(worker_task, timeout=_WORKER_DRAIN_TIMEOUT, label="worker")

    if polling_task is not None:
        if dispatcher is not None:
            await _request_stop_polling(dispatcher)
        await _finish(polling_task, timeout=_POLLING_STOP_TIMEOUT, label="polling")

    # The heartbeat goes last so the container keeps reporting healthy until it
    # has genuinely finished.
    heartbeat_task.cancel()
    await asyncio.gather(heartbeat_task, return_exceptions=True)

    with contextlib.suppress(Exception):
        await provider.aclose()
    with contextlib.suppress(Exception):
        await bot.session.close()
    await dispose_engine()

    log.info("shutdown complete")


async def _finish(task: asyncio.Task[None], *, timeout: float, label: str) -> None:
    """Wait for a task, then cancel it if it will not stop on its own."""
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("%s did not stop in time; cancelling", label, extra={"timeout": timeout})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - a teardown must not raise
        log.exception("%s stopped with an error", label)


async def _request_stop_polling(dispatcher: Dispatcher) -> None:
    """Ask the dispatcher to stop fetching updates.

    `Dispatcher.stop_polling()` is synchronous in most aiogram 3.x releases and a
    coroutine in others. Awaiting only an awaitable result covers both, which
    costs one `inspect` call on a path that runs once per container lifetime.
    """
    stop = getattr(dispatcher, "stop_polling", None)
    if stop is None:  # pragma: no cover - every 3.x release has it
        return
    try:
        result = stop()
    except Exception as exc:  # noqa: BLE001 - falling back to cancellation is fine
        log.debug("stop_polling() raised; will cancel the task instead", extra={"error": str(exc)})
        return
    if inspect.isawaitable(result):
        await result


if __name__ == "__main__":
    raise SystemExit(main())
