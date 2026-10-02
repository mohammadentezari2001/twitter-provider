"""Handler routers, composed in the order they are matched.

Order is significant. Commands come first so that a `/status` typed while
discussing a link is not treated as prose; callbacks come before the catch-all
message handler because they are a different update type entirely; the link
handler is last because its filter — "any text that is not a command" — is the
broadest.
"""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import callbacks, commands, links


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(commands.router)
    root.include_router(callbacks.router)
    root.include_router(links.router)
    return root


__all__ = ["build_router", "callbacks", "commands", "links"]
