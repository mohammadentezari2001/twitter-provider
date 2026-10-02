"""The background worker: the only thing in the process that talks to the network
on a schedule.

`JobRunner` knows how to execute one job; `Worker` knows how to find them and how
many may run at once. Keeping those apart is what lets the handlers be tested
with a fake bot and no loop at all — `JobRunner.process(job_id)` is directly
callable from a test.

In `RUN_MODE=all` the bot process runs a `Worker` in the same event loop as the
dispatcher. Nothing here assumes that: the queue is claimed with
`FOR UPDATE SKIP LOCKED`, so a second worker container alongside it is a
configuration change, not a code change.
"""

from __future__ import annotations

from app.worker.jobs import JobRunner
from app.worker.runner import Worker

__all__ = ["JobRunner", "Worker"]
