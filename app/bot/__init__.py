"""Telegram-facing layer.

Everything that knows about aiogram lives under this package. The services,
repositories and providers below it do not import from here, which is what keeps
the publish path testable without a bot token.
"""

from __future__ import annotations

from app.bot.context import BotContext

__all__ = ["BotContext"]
