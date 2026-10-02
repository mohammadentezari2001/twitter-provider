"""Control-flow exceptions used *between* services and the worker.

These are not errors. They are how a service tells the worker "not now" or "this
will never work", without the service needing to know that a job queue exists.
The worker in `app/worker/` translates them into job state transitions.
"""

from __future__ import annotations


class JobDeferral(Exception):
    """Put the job back on the queue without spending an attempt.

    Raised when the work is still valid but the moment is wrong — a publish
    cooldown still ticking, or a provider that told us to come back in N seconds.

    The distinction from a failure is deliberate and load-bearing. Counting
    deferrals as failures would let a busy channel exhaust `max_attempts` and
    bury a job that never actually failed, which is exactly the kind of bug that
    only appears under load.
    """

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason or f"deferred for {seconds:.1f}s")
        self.seconds = max(0.0, float(seconds))
        self.reason = reason


class PermanentJobError(Exception):
    """The job cannot succeed and retrying will not change that.

    Raised for conditions the worker should record and move past immediately: an
    unsupported job type, or a payload that is structurally wrong. Without this,
    a misconfigured job would consume its whole retry budget to reach the same
    conclusion more slowly.

    `retryable` matches the attribute on `ProviderError`, `MediaError` and
    `PublishError` so the worker can read one attribute off any of them.
    """

    retryable = False
    retry_after: float | None = None
