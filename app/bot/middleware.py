"""Middleware: correlation IDs, the admin gate, and the per-update session.

Order matters, and it is not the obvious one.

1. **Correlation** (outermost). Every log line the update produces — including
   lines from the admin check that rejects it — must carry the same ID, so this
   has to be the first thing that runs.
2. **Admin gate.** This needs the database, so it opens its own short session
   rather than relying on one the handler will open later. A rejected update must
   be rejected *before* a handler gets a session, or a stranger's message would
   hold a transaction open for the duration of its own rejection.
3. **Session** (inner). Only reached by updates that passed the gate, and only
   wraps the handler — the transaction is not held open across nothing.

The alternative — one session opened in the outermost middleware — is shorter,
but it means every unauthenticated message from the internet holds a database
connection for as long as it takes to reply "not authorised".
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import messages
from app.config import Settings
from app.database.repositories.admins import AdminRepository
from app.database.session import session_scope
from app.logging_config import correlation_context, get_logger

log = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _user(event: TelegramObject, data: dict[str, Any]) -> User | None:
    direct = data.get("event_from_user")
    if isinstance(direct, User):
        return direct
    if isinstance(event, Message):
        return event.from_user
    if isinstance(event, CallbackQuery):
        return event.from_user
    return None


class CorrelationMiddleware(BaseMiddleware):
    """Give every update a correlation ID for the duration of its handling."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        update_id = getattr(event, "update_id", None)
        # Derived from the update ID so that everything logged while handling one
        # update — in the bot process and, later, in any job it enqueues — is
        # greppable by a single token.
        correlation = uuid.uuid5(uuid.NAMESPACE_OID, f"telegram-update:{update_id}")
        with correlation_context(correlation):
            return await handler(event, data)


class AdminGateMiddleware(BaseMiddleware):
    """Reject anyone who is not an active administrator.

    Silence would be a defensible choice — it reveals nothing about whether the
    bot exists — but a person who added the bot and got no reply at all has no
    way to discover that they need to be on a list. The reply names the command
    that tells them their own ID, which is the one fact they need.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = _user(event, data)
        if user is None:
            return await handler(event, data)

        allowed = False
        async with session_scope(self._settings) as session:
            admins = AdminRepository(session)
            allowed = await admins.is_active_admin(user.id)
            if allowed:
                # Denormalised onto the row so `/status` can show who has used the
                # bot without a separate audit table.
                await admins.touch_last_seen(
                    user.id, username=user.username, display_name=user.full_name
                )

        if not allowed:
            log.info(
                "rejected a non-admin update",
                extra={"user_id": user.id, "username": user.username},
            )
            await _reject(event)
            return None

        data["is_admin"] = True
        return await handler(event, data)


async def _reject(event: TelegramObject) -> None:
    if isinstance(event, Message):
        try:
            await event.answer(messages.NOT_AUTHORISED, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001 - rejection must never raise
            log.warning("could not send the not-authorised reply", extra={"error": str(exc)})
    elif isinstance(event, CallbackQuery):
        try:
            await event.answer(messages.ADMIN_ONLY_CALLBACK, show_alert=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not answer an unauthorised callback", extra={"error": str(exc)})


class SessionMiddleware(BaseMiddleware):
    """One transaction per update, committed when the handler returns.

    Committing on the way out is what makes a handler's database work atomic: the
    submission row, the enqueued job and the status message either all land or
    none do.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with session_scope(self._settings) as session:
            data["session"] = session
            return await handler(event, data)


class ErrorMiddleware(BaseMiddleware):
    """Last line of defence: log, apologise, never propagate.

    An unhandled exception in a handler would otherwise be logged by aiogram and
    leave the admin staring at a message that got no reply at all, with nothing
    to distinguish "the bot is broken" from "the bot ignored me".
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001 - this is the catch-all, by design
            log.exception("unhandled error while processing an update", extra={"error": str(exc)})
            await _apologise(event)
            return None


async def _apologise(event: TelegramObject) -> None:
    try:
        if isinstance(event, Message):
            await event.answer(messages.UNEXPECTED_ERROR, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.answer(messages.UNEXPECTED_ERROR, show_alert=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not send the error reply", extra={"error": str(exc)})


__all__ = [
    "AdminGateMiddleware",
    "CorrelationMiddleware",
    "ErrorMiddleware",
    "SessionMiddleware",
    "session_of",
]


def session_of(data: dict[str, Any]) -> AsyncSession:
    """Fetch the per-update session, failing loudly if the middleware is missing.

    A `KeyError` deep inside a handler is a worse diagnostic than this.
    """
    session = data.get("session")
    if not isinstance(session, AsyncSession):  # pragma: no cover - wiring error
        raise RuntimeError("SessionMiddleware is not registered for this update type")
    return session
