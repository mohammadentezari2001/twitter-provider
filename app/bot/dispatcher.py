"""Dispatcher assembly.

Middleware ordering is the only subtle part, and it is subtle in a way that is
easy to get backwards. In aiogram, the **first** outer middleware registered is
the outermost — so it wraps everything below it.

The order chosen here:

    Correlation → Error → AdminGate → (filters) → Session → handler

`Correlation` is outermost so that the ID is active for *every* log line the
update produces, including the ones written by `Error` when a handler explodes.
Registering them the other way round looks equivalent and is not: the correlation
context is exited as the exception propagates out of it, so the error log — the
one line you most want to grep — would be the only one missing the ID.

`Error` sits above the gate so that a database failure inside the admin check is
also caught and logged, rather than reaching aiogram's default handler.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot.context import BotContext
from app.bot.handlers import build_router
from app.bot.middleware import (
    AdminGateMiddleware,
    CorrelationMiddleware,
    ErrorMiddleware,
    SessionMiddleware,
)
from app.config import Settings
from app.logging_config import get_logger

log = get_logger(__name__)

COMMANDS = [
    BotCommand(command="status", description="Queue, worker and provider health"),
    BotCommand(command="stats", description="Today and the last 7 days"),
    BotCommand(command="recent", description="The last few publications"),
    BotCommand(command="id", description="Show your Telegram user ID"),
    BotCommand(command="help", description="How to use the bot"),
]


def build_dispatcher(settings: Settings, ctx: BotContext) -> Dispatcher:
    """Build the dispatcher and wire in middleware and routers.

    `MemoryStorage` is correct here rather than a shortcoming: no FSM state is
    used. Pending previews live in the `submissions` table precisely so they
    survive a restart, which is the thing FSM storage would have been for.
    """
    dispatcher = Dispatcher(storage=MemoryStorage())

    # Workflow data, injected into every handler's keyword arguments. This is how
    # `ctx` and `settings` reach handlers without a module-level global.
    dispatcher["ctx"] = ctx
    dispatcher["settings"] = settings

    dispatcher.update.outer_middleware(CorrelationMiddleware())
    dispatcher.update.outer_middleware(ErrorMiddleware())

    # Registered per observer rather than on `update`, because both need the
    # concrete event type to reply to a rejection.
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.outer_middleware(AdminGateMiddleware(settings))
        observer.middleware(SessionMiddleware(settings))

    dispatcher.include_router(build_router())
    return dispatcher


async def register_commands(bot: Bot) -> None:
    """Publish the command list to Telegram's UI. Cosmetic, never fatal."""
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as exc:  # noqa: BLE001 - a missing menu must not stop startup
        log.warning("could not register the bot command list", extra={"error": str(exc)})
