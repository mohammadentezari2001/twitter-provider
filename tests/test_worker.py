"""Worker tests: the claim loop, its concurrency limits, and error translation.

Everything here runs against fakes. The point is not to prove that PostgreSQL
hands out jobs correctly — that is `FOR UPDATE SKIP LOCKED`, and the integration
tests cover it — but to prove the code *around* it does the right thing: that a
job's concurrency slot is always released, that a deferral does not consume an
attempt, that a permanent error does not burn the whole retry budget, and that
the loop stops when it is told to.

The notice helpers are tested directly as well, because "how many messages does a
flaky provider send the admin?" is a product decision that is easy to get wrong
and trivial to assert.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.config import Settings
from app.database.enums import (
    JobStatus,
    JobType,
    PublicationStatus,
)
from app.database.models import Job
from app.services.errors import JobDeferral, PermanentJobError
from app.services.publisher import PublishOutcome
from app.worker import JobRunner, Worker
from app.worker import jobs as jobs_module
from app.worker import runner as runner_module
from app.worker.jobs import (
    _failure_notice,
    _int_or_none,
    _payload,
    _publish_notice,
    _type_of,
    _uuid_or_none,
)


# --------------------------------------------------------------------- fixtures
class FakeBot:
    """Records what would have been sent, and can be made to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self._fail = fail
        self.next_message_id = 1000

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        if self._fail:
            raise RuntimeError("telegram is down")
        self.sent.append((chat_id, text))
        self.next_message_id += 1
        return type("Message", (), {"message_id": self.next_message_id})()

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: Any
    ) -> Any:
        if self._fail:
            raise RuntimeError("telegram is down")
        self.edits.append((chat_id, message_id, text))
        return type("Message", (), {"message_id": message_id})()


class FakeHeartbeat:
    def __init__(self) -> None:
        self.beats = 0

    def touch(self) -> None:
        self.beats += 1

    async def run(self, interval: float, stop: asyncio.Event) -> None:  # pragma: no cover
        await stop.wait()


def make_job(**overrides: Any) -> Job:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "type": JobType.FETCH_TWEET,
        "status": JobStatus.RUNNING,
        "payload": {"tweet_id": "1234567890", "chat_id": -100999},
        "attempts": 1,
        "max_attempts": 5,
        "chat_id": -100999,
        "requested_by": 111111111,
    }
    base.update(overrides)
    return Job(**base)


# ------------------------------------------------------------------ pure helpers
def test_type_of_accepts_both_the_enum_and_a_bare_string() -> None:
    assert _type_of(make_job(type=JobType.PUBLISH_TWEET)) is JobType.PUBLISH_TWEET
    assert _type_of(make_job(type="publish_tweet")) is JobType.PUBLISH_TWEET


def test_payload_tolerates_a_non_mapping_column() -> None:
    assert _payload(make_job(payload={"a": 1})) == {"a": 1}
    assert _payload(make_job(payload=None)) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        (-100123, -100123),
        ("42", 42),
        ("-100123", -100123),
        (None, None),
        ("x", None),
        # A bool is an int in Python; `True` arriving from JSONB must not become
        # a chat ID of 1.
        (True, None),
    ],
)
def test_int_or_none(value: object, expected: int | None) -> None:
    assert _int_or_none(value) == expected


def test_uuid_or_none_rejects_junk_without_raising() -> None:
    good = uuid.uuid4()
    assert _uuid_or_none(str(good)) == good
    assert _uuid_or_none(good) == good
    assert _uuid_or_none("not-a-uuid") is None
    assert _uuid_or_none(None) is None


def _outcome(**overrides: Any) -> PublishOutcome:
    base: dict[str, Any] = {
        "publication_id": uuid.uuid4(),
        "status": PublicationStatus.PUBLISHED,
        "message_ids": (10,),
        "caption": "caption",
        "media_sent": 1,
        "media_failed": 0,
        "skipped_media": 0,
        "message_url": "https://t.me/c/123/10",
    }
    base.update(overrides)
    return PublishOutcome(**base)


def test_publish_notice_reports_success_with_the_link() -> None:
    notice = _publish_notice(_outcome())

    assert notice is not None
    assert "Published" in notice
    assert "https://t.me/c/123/10" in notice


def test_publish_notice_explains_a_partial_publish() -> None:
    notice = _publish_notice(
        _outcome(status=PublicationStatus.PARTIAL, media_failed=1, skipped_media=2)
    )

    assert notice is not None
    assert "not everything" in notice
    assert "1 media file" in notice
    assert "2 item" in notice


def test_publish_notice_is_silent_for_a_status_it_does_not_describe() -> None:
    assert _publish_notice(_outcome(status=PublicationStatus.PENDING)) is None


# -------------------------------------------------------------------- notices
def test_a_terminal_failure_notifies_once() -> None:
    notify = _failure_notice(make_job(), JobStatus.DEAD, "ProviderTransientError: 503")

    assert notify is not None
    chat_id, text = notify
    assert chat_id == -100999
    assert "Could not fetch" in text


def test_a_retryable_first_failure_says_it_will_be_retried() -> None:
    notify = _failure_notice(make_job(attempts=1), JobStatus.PENDING, "boom")

    assert notify is not None
    assert "retried automatically" in notify[1]


def test_later_retries_do_not_message_the_admin_again() -> None:
    """A flaky provider must not turn into a chat full of identical warnings."""
    assert _failure_notice(make_job(attempts=3), JobStatus.PENDING, "boom") is None


def test_a_failed_publish_uses_the_publish_wording() -> None:
    notify = _failure_notice(
        make_job(type=JobType.PUBLISH_TWEET), JobStatus.FAILED, "PublishError: nope"
    )

    assert notify is not None
    assert "Publishing failed" in notify[1]


def test_a_job_with_no_chat_cannot_notify() -> None:
    assert _failure_notice(make_job(chat_id=None), JobStatus.DEAD, "boom") is None


def test_the_notice_escapes_an_error_that_contains_markup() -> None:
    notify = _failure_notice(make_job(), JobStatus.DEAD, "<b>not markup</b> & co")

    assert notify is not None
    assert "&lt;b&gt;" in notify[1]


# ----------------------------------------------------------------- job running
@contextlib.asynccontextmanager
async def _fake_scope(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    yield "session"


class _FakeJobRepo:
    """Stands in for `JobRepository`, recording the state transitions."""

    calls: list[tuple[str, Any]] = []

    def __init__(self, session: Any) -> None:
        self._session = session

    async def mark_succeeded(self, job: Job, *, result: Any = None) -> Job:
        type(self).calls.append(("succeeded", result))
        job.status = JobStatus.SUCCEEDED
        return job

    async def mark_failed(self, job: Job, error: str, **kwargs: Any) -> JobStatus:
        type(self).calls.append(("failed", kwargs.get("retryable", True)))
        status = JobStatus.PENDING if kwargs.get("retryable", True) else JobStatus.FAILED
        job.status = status
        return status

    async def defer(self, job: Job, *, seconds: float, reason: str | None = None) -> Job:
        type(self).calls.append(("deferred", seconds))
        job.status = JobStatus.PENDING
        return job


class _FakeSession:
    def __init__(self, job: Job | None) -> None:
        self._job = job

    async def get(self, model: Any, key: Any, **kwargs: Any) -> Job | None:
        return self._job


class StubRunner(JobRunner):
    """A `JobRunner` whose work is decided by the test."""

    def __init__(
        self,
        settings: Settings,
        bot: Any,
        ctx: Any,
        *,
        result: Any = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(settings, bot, ctx)
        self._result = result if result is not None else {"status": "ok"}
        self._error = error

    async def _dispatch(self, session: Any, job: Job) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return dict(self._result)


@pytest.fixture
def patched_jobs(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Any:
    """Point `app.worker.jobs` at fakes, and hand back the job under test."""
    _FakeJobRepo.calls = []
    job = make_job()

    @contextlib.asynccontextmanager
    async def scope(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield _FakeSession(job)

    monkeypatch.setattr(jobs_module, "session_scope", scope)
    monkeypatch.setattr(jobs_module, "JobRepository", _FakeJobRepo)
    return job


async def test_a_successful_job_is_recorded_and_its_notice_sent_after_commit(
    settings: Settings, patched_jobs: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    runner = StubRunner(
        settings, bot, None, result={"status": "preview", "notify": (555, "hello")}
    )

    await runner.process(patched_jobs.id)

    assert _FakeJobRepo.calls == [("succeeded", {"status": "preview"})]
    # `notify` is popped before the result is stored, so the JSONB column never
    # sees a tuple, and the message still goes out.
    assert bot.sent == [(555, "hello")]


async def test_a_failing_notification_does_not_fail_the_job(
    settings: Settings, patched_jobs: Job
) -> None:
    bot = FakeBot(fail=True)
    runner = StubRunner(settings, bot, None, result={"status": "ok", "notify": (1, "x")})

    await runner.process(patched_jobs.id)

    assert ("succeeded", {"status": "ok"}) in _FakeJobRepo.calls


async def test_a_deferral_reschedules_without_marking_the_job_failed(
    settings: Settings, patched_jobs: Job
) -> None:
    runner = StubRunner(settings, FakeBot(), None, error=JobDeferral(12.5, "cooldown"))

    await runner.process(patched_jobs.id)

    assert _FakeJobRepo.calls == [("deferred", 12.5)]


async def test_a_permanent_error_is_not_retried(
    settings: Settings, patched_jobs: Job
) -> None:
    runner = StubRunner(settings, FakeBot(), None, error=PermanentJobError("no handler"))

    await runner.process(patched_jobs.id)

    assert _FakeJobRepo.calls == [("failed", False)]


async def test_a_provider_error_carries_its_own_retryability(
    settings: Settings, patched_jobs: Job
) -> None:
    from app.providers.base import ProviderTransientError

    runner = StubRunner(settings, FakeBot(), None, error=ProviderTransientError("503"))

    await runner.process(patched_jobs.id)

    assert _FakeJobRepo.calls == [("failed", True)]


async def test_a_missing_job_is_not_an_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled between the claim and the run — nothing to do, nothing to log as a failure."""

    @contextlib.asynccontextmanager
    async def scope(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield _FakeSession(None)

    monkeypatch.setattr(jobs_module, "session_scope", scope)
    monkeypatch.setattr(jobs_module, "JobRepository", _FakeJobRepo)
    _FakeJobRepo.calls = []

    await StubRunner(settings, FakeBot(), None).process(uuid.uuid4())

    assert _FakeJobRepo.calls == []


# ------------------------------------------------------------------ claim loop
class FakeQueue:
    """A queue that hands out pre-loaded jobs and counts concurrent runs."""

    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = list(jobs)
        self.claimed: list[JobType] = []
        self.reclaimed = 0

    async def claim(self, *, locked_by: str, types: list[JobType]) -> Job | None:
        for index, job in enumerate(self._jobs):
            job_type = job.type if isinstance(job.type, JobType) else JobType(job.type)
            if job_type in types:
                self._jobs.pop(index)
                self.claimed.append(job_type)
                return job
        return None

    async def reclaim_expired(self, *, timeout_seconds: float) -> int:
        return self.reclaimed


class ConcurrencyProbe(JobRunner):
    """Runs instantly, but records the highest concurrency actually reached."""

    def __init__(self, settings: Settings, *, on_run: Any = None) -> None:
        super().__init__(settings, FakeBot(), None)
        self.live = 0
        self.peak = 0
        self.seen: list[uuid.UUID] = []
        self._on_run = on_run

    async def _dispatch(self, session: Any, job: Job) -> dict[str, Any]:
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.seen.append(job.id)
        try:
            await asyncio.sleep(0)
            if self._on_run is not None:
                self._on_run()
            return {"status": "ok"}
        finally:
            self.live -= 1


@pytest.fixture
def patched_runner(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the queue and the maintenance sweep inside `app.worker.runner`."""
    state: dict[str, Any] = {}

    class _Repo:
        def __init__(self, session: Any) -> None:
            pass

        async def claim(self, *, locked_by: str, types: list[JobType]) -> Job | None:
            return await state["queue"].claim(locked_by=locked_by, types=types)

        async def reclaim_expired(self, *, timeout_seconds: float) -> int:
            return await state["queue"].reclaim_expired(timeout_seconds=timeout_seconds)

    class _Submissions:
        def __init__(self, session: Any) -> None:
            pass

        async def expire_stale(self, **kwargs: Any) -> int:
            return 0

    class _Tweets:
        def __init__(self, session: Any) -> None:
            pass

        async def prune_expired(self, **kwargs: Any) -> int:
            return 0

    @contextlib.asynccontextmanager
    async def scope(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield object()

    monkeypatch.setattr(runner_module, "session_scope", scope)
    monkeypatch.setattr(runner_module, "JobRepository", _Repo)
    monkeypatch.setattr(runner_module, "SubmissionRepository", _Submissions)
    monkeypatch.setattr(runner_module, "TweetRepository", _Tweets)
    monkeypatch.setattr(runner_module, "prune_stale_dirs", lambda *a, **k: 0)
    return state


def worker_settings(settings: Settings, **updates: Any) -> Settings:
    base = {
        "WORKER_POLL_INTERVAL": 0.01,
        "MAX_CONCURRENT_FETCHES": 1,
        "MAX_CONCURRENT_PUBLISHES": 1,
    }
    return settings.model_copy(update={**base, **updates})


async def test_the_loop_claims_types_the_runner_handles(settings: Settings) -> None:
    config = worker_settings(settings, MAX_CONCURRENT_FETCHES=3, MAX_CONCURRENT_PUBLISHES=2)
    worker = Worker(config, ConcurrencyProbe(config), FakeHeartbeat())

    types = worker._claimable_types()

    assert set(types) == {JobType.FETCH_TWEET, JobType.PUBLISH_TWEET}
    # `download_media` has no handler, so it must never be claimed.
    assert JobType.DOWNLOAD_MEDIA not in types


async def test_a_saturated_type_is_not_claimed(settings: Settings) -> None:
    config = worker_settings(settings, MAX_CONCURRENT_FETCHES=1, MAX_CONCURRENT_PUBLISHES=1)
    worker = Worker(config, ConcurrencyProbe(config), FakeHeartbeat())
    worker._inflight[JobType.FETCH_TWEET] = 1

    assert worker._claimable_types() == [JobType.PUBLISH_TWEET]


async def test_every_queued_job_is_run_and_the_loop_stops(
    settings: Settings, patched_runner: dict[str, Any]
) -> None:
    config = worker_settings(settings)
    jobs = [make_job(id=uuid.uuid4()) for _ in range(3)]
    patched_runner["queue"] = FakeQueue(jobs)

    stop = asyncio.Event()
    probe = ConcurrencyProbe(config, on_run=lambda: stop.set() if len(probe.seen) >= 3 else None)
    worker = Worker(config, probe, FakeHeartbeat())

    await asyncio.wait_for(worker.run(stop), timeout=10)

    assert len(probe.seen) == 3
    assert worker.inflight == 0


async def test_concurrency_never_exceeds_the_configured_limit(
    settings: Settings, patched_runner: dict[str, Any]
) -> None:
    """The limit is the whole reason the counters exist; it must actually hold."""
    config = worker_settings(settings, MAX_CONCURRENT_FETCHES=2, MAX_CONCURRENT_PUBLISHES=1)
    jobs = [make_job(id=uuid.uuid4()) for _ in range(6)]
    patched_runner["queue"] = FakeQueue(jobs)

    stop = asyncio.Event()
    probe = ConcurrencyProbe(config, on_run=lambda: stop.set() if len(probe.seen) >= 6 else None)
    worker = Worker(config, probe, FakeHeartbeat())

    # A small sleep in each job is what makes an over-limit claim observable.
    await asyncio.wait_for(worker.run(stop), timeout=10)

    assert probe.peak <= 2
    assert len(probe.seen) == 6


async def test_the_loop_exits_promptly_when_asked_to_stop(
    settings: Settings, patched_runner: dict[str, Any]
) -> None:
    config = worker_settings(settings, WORKER_POLL_INTERVAL=5.0)
    patched_runner["queue"] = FakeQueue([])

    stop = asyncio.Event()
    heartbeat = FakeHeartbeat()
    worker = Worker(config, ConcurrencyProbe(config), heartbeat)

    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert heartbeat.beats >= 1


async def test_a_stuck_job_is_cancelled_during_the_drain(
    settings: Settings, patched_runner: dict[str, Any]
) -> None:
    """A job that will not finish keeps its slot until the grace period expires.

    Cancelling it leaves the row `running` with a stale `locked_at`, which is
    exactly the state `reclaim_expired` is written to recover — so the assertion
    that matters is that the slot is released and the task really is cancelled.
    """
    config = worker_settings(settings)
    worker = Worker(config, ConcurrencyProbe(config), FakeHeartbeat())

    stuck: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(3600))
    worker._tasks.add(stuck)
    stuck.add_done_callback(worker._tasks.discard)
    worker._inflight[JobType.FETCH_TWEET] = 1

    await asyncio.wait_for(worker._drain(grace=0.05), timeout=5)

    assert stuck.cancelled()
