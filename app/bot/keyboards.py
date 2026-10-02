"""Inline keyboards.

Callback data is typed with aiogram's `CallbackData` factory rather than raw
strings. Two reasons: a typo in an action name becomes an import-time or
call-time error instead of a button that silently does nothing, and the payload
is length-checked — Telegram caps `callback_data` at 64 bytes, and a hand-built
f-string with a UUID in it passes 40 bytes before anyone notices it is close.
"""

from __future__ import annotations

from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class PreviewAction(CallbackData, prefix="pv"):
    """`pv:publish:<submission-uuid>` or `pv:cancel:<submission-uuid>`."""

    action: Literal["publish", "cancel"]
    submission_id: str


def preview_keyboard(submission_id: str, *, source_url: str | None = None) -> InlineKeyboardMarkup:
    """Publish / Cancel, with an optional link to the original post.

    Cancel is placed first so that the destructive-coded action is not the one
    sitting under the thumb; Publish is not styled as `primary` because in a
    two-button confirmation the visual weight would imply a default that does not
    exist.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🚫 Cancel",
                callback_data=PreviewAction(
                    action="cancel", submission_id=submission_id
                ).pack(),
            ),
            InlineKeyboardButton(
                text="✅ Publish",
                callback_data=PreviewAction(
                    action="publish", submission_id=submission_id
                ).pack(),
            ),
        ]
    ]
    if source_url:
        rows.append([InlineKeyboardButton(text="🔗 Open original", url=source_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def resolved_keyboard(*, source_url: str | None = None, note: str = "handled") -> InlineKeyboardMarkup:
    """What replaces the buttons once a preview has been acted on.

    The buttons are removed rather than left in place and made to answer "already
    handled" — a stale Publish button that does nothing when pressed is worse
    than no button, because it invites a second press.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if source_url:
        rows.append([InlineKeyboardButton(text="🔗 Open original", url=source_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
