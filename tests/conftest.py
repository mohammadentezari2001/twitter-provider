"""Shared fixtures.

Nothing here touches the network or a database, and neither does any test marked
other than `integration`. The whole suite must run in a process with no services
available — that is the invariant `app.config` states for `Settings`, and it is
what keeps the fast tests fast.

Tests needing a live PostgreSQL are marked `integration` and skip unless
`TEST_DATABASE_URL` is set (the marker is registered in `pyproject.toml`).
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

requires_database = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; integration tests need a live PostgreSQL",
)

#: A syntactically valid bot token that satisfies `TOKEN_RE`, so fixtures can
#: build a `Settings` that passes its own validation. Not a real credential.
FAKE_BOT_TOKEN = "123456789:" + "A" * 35


@pytest.fixture
def settings() -> Settings:
    """A populated `Settings` that never reads `.env`.

    `_env_file=None` matters: without it a developer's real `.env` would leak in
    and tests would pass or fail depending on the machine. Environment variables
    are still honoured, which is intentional — CI passes configuration that way.
    """
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN=FAKE_BOT_TOKEN,
        TELEGRAM_CHANNEL_ID="-1001234567890",
        ADMIN_USER_IDS="111111111,222222222",
    )
