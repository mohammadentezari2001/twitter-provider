"""The shared runtime state a bot process needs, passed to handlers via `data`.

Handlers receive this as the `ctx` argument rather than reaching for module-level
globals, which is what lets an integration test build a dispatcher with a fake
publisher and assert on what the handler actually did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings
from app.providers.base import TweetProvider
from app.services.fetcher import TweetFetcher
from app.services.publisher import Publisher
from app.utils.timeutil import utcnow


@dataclass(slots=True)
class BotContext:
    settings: Settings
    provider: TweetProvider
    fetcher: TweetFetcher
    publisher: Publisher
    #: Resolved numeric channel ID. `TELEGRAM_CHANNEL_ID` may be `@username`,
    #: which is not something that can be stored in `publications.channel_id`.
    channel_id: int
    started_at: datetime = field(default_factory=utcnow)
    #: Set by `app.main` so `/status` can report whether the worker is alive.
    worker_seen_at: datetime | None = None

    @property
    def uptime_seconds(self) -> float:
        return (utcnow() - self.started_at).total_seconds()
