"""Retry/backoff arithmetic.

Uses *equal jitter* — half the computed delay plus a random share of the other
half. Unlike full jitter it keeps a meaningful lower bound, which matters when
backing off from a rate limit, while still de-synchronising concurrent workers.
"""

from __future__ import annotations

import asyncio
import random  # noqa: S311 - jitter is not security-sensitive
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TypeVar

from app.utils.timeutil import utcnow

T = TypeVar("T")


def compute_backoff(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: bool = True,
) -> float:
    """Delay before retry number `attempt` (1 = first retry).

    `base_delay * 2 ** (attempt - 1)`, clamped to `max_delay`, then jittered.
    """
    if attempt < 1:
        attempt = 1
    # Clamp the exponent so 2 ** attempt cannot overflow on absurd inputs.
    exponent = min(attempt - 1, 32)
    raw = min(base_delay * (2.0**exponent), max_delay)
    if not jitter:
        return raw
    half = raw / 2.0
    return half + random.random() * half


def next_attempt_at(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    now: datetime | None = None,
    override_delay: float | None = None,
) -> datetime:
    """Absolute UTC timestamp for the next attempt.

    `override_delay` wins when a server told us exactly how long to wait
    (Telegram `RetryAfter`, HTTP `Retry-After`).
    """
    delay = (
        max(override_delay, 0.0)
        if override_delay is not None
        else compute_backoff(attempt, base_delay=base_delay, max_delay=max_delay)
    )
    return (now or utcnow()) + timedelta(seconds=delay)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    retry_on: tuple[type[BaseException], ...],
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call `operation` until it succeeds or `attempts` is exhausted.

    Intended for short in-process retries (a flaky HTTP GET). Durable,
    cross-restart retries are handled by the job queue instead.
    """
    last: BaseException | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            return await operation()
        except retry_on as exc:
            last = exc
            if attempt >= attempts:
                break
            delay = compute_backoff(attempt, base_delay=base_delay, max_delay=max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)
    assert last is not None
    raise last
