"""Resolving and validating the target channel.

`TELEGRAM_CHANNEL_ID` accepts either a numeric `-100...` ID or an `@username`.
Only the numeric form fits in `publications.channel_id`, and only the numeric
form is stable — a channel can be renamed, and its username changed with it. So
the username is resolved to an ID once at startup via `getChat`, and every layer
below this one works exclusively in integers.

The permission check is a startup concern on purpose. Discovering at the first
publish that the bot is not a channel administrator means the failure surfaces as
a `ChatWriteForbidden` after an admin has already pasted a link and pressed
Publish — the worst possible moment. Checking at boot turns it into a log line
before anything else happens.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import ConfigurationError, Settings
from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    channel_id: int
    title: str
    username: str | None
    #: `True` when the bot may actually post. Reported at startup, never fatal —
    #: a permission problem is fixable in Telegram without a redeploy.
    can_post: bool
    is_admin: bool
    permission_detail: str | None = None


async def resolve_channel_id(bot: Bot, settings: Settings) -> int:
    """Turn `TELEGRAM_CHANNEL_ID` into a numeric ID.

    Raises:
        ConfigurationError: if the channel cannot be resolved. This is fatal: the
            application cannot do anything useful without a destination.
    """
    raw = settings.TELEGRAM_CHANNEL_ID.strip()
    if not raw:
        raise ConfigurationError("TELEGRAM_CHANNEL_ID is not set")

    if raw.lstrip("-").isdigit():
        return int(raw)

    reference = raw if raw.startswith("@") else f"@{raw}"
    try:
        chat = await bot.get_chat(reference)
    except TelegramAPIError as exc:
        raise ConfigurationError(
            f"could not resolve TELEGRAM_CHANNEL_ID={reference}: {exc}. "
            "Use the numeric -100... ID (forward a channel post to @userinfobot) "
            "if the username keeps failing."
        ) from exc
    return chat.id


async def inspect_channel(bot: Bot, channel_id: int) -> ChannelInfo:
    """Check that the bot can actually post, for a startup log line.

    Never raises: a failure to *inspect* is not a failure to publish, and a
    transient Telegram wobble at boot should not stop the process from starting.
    """
    title = str(channel_id)
    username: str | None = None
    try:
        chat = await bot.get_chat(channel_id)
        title = chat.title or title
        username = chat.username
    except TelegramAPIError as exc:
        log.warning("could not read channel details", extra={"error": str(exc)})

    try:
        member = await bot.get_chat_member(channel_id, bot.id)
    except TelegramAPIError as exc:
        return ChannelInfo(
            channel_id=channel_id,
            title=title,
            username=username,
            can_post=False,
            is_admin=False,
            permission_detail=f"could not read bot membership: {exc}",
        )

    status = str(getattr(member, "status", ""))
    is_admin = status in ("administrator", "creator")
    # `can_post_messages` is only present on administrator memberships; a creator
    # has it implicitly and Telegram omits the field.
    can_post = is_admin and (
        status == "creator" or bool(getattr(member, "can_post_messages", False))
    )

    detail = None
    if not is_admin:
        detail = "the bot is not an administrator of the channel"
    elif not can_post:
        detail = "the bot is an administrator but lacks the 'Post Messages' permission"

    return ChannelInfo(
        channel_id=channel_id,
        title=title,
        username=username,
        can_post=can_post,
        is_admin=is_admin,
        permission_detail=detail,
    )
