"""ORM models for every durable object in the relay.

Layout note: the tables live in one module rather than a `models/` package
because they are a single, tightly-coupled schema — `Publication` is only
meaningful next to `Tweet`, and the partial unique indexes that stop duplicate
posts are best reviewed alongside the columns they constrain. This mirrors the
existing style (`app/config.py` is one 400-line module for one concern).

Relationships use `lazy="raise_on_sql"`, which is the correct default for async
SQLAlchemy: accessing an unloaded relationship raises immediately with a clear
message instead of the obscure `MissingGreenlet` that a lazy load produces on a
worker thread. Call sites must eager-load explicitly (`selectinload`) or query
the child table directly, which the repositories do.

The design rule behind the whole schema: **PostgreSQL is the source of truth for
all durable state.** `REDIS_URL` is optional by configuration, so Redis is only
ever a cache or fast path layered on top — never the only copy of something the
application cannot rebuild. Job queue, pending confirmations and the publish
cooldown all live here, which is why behaviour is identical with Redis on or off.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import TWEET_ID_MAX_DIGITS
from app.database.base import Base, TelegramId, TimestampMixin, TZDateTime, UuidPrimaryKeyMixin
from app.database.enums import (
    AdminRole,
    JobStatus,
    JobType,
    MediaKind,
    PublicationMode,
    PublicationStatus,
    SubmissionStatus,
    enum_type,
)

#: Schema-level fallback for `Job.max_attempts`, mirroring the default of
#: `Settings.MAX_RETRIES`. The repository always passes the configured value;
#: this only exists so a row inserted by hand is still sane.
DEFAULT_MAX_ATTEMPTS = 5

__all__ = [
    "Admin",
    "DailyStat",
    "Job",
    "MediaItem",
    "Publication",
    "PublishThrottle",
    "Submission",
    "Tweet",
]


class Admin(Base, TimestampMixin):
    """A Telegram user permitted to drive the bot.

    Seeded at startup from `Settings.ADMIN_USER_IDS` by upsert, with
    `Settings.owner_id` (the lowest configured ID) promoted to `OWNER`. Rows are
    never deleted by that sync, so a grant made at runtime is not silently
    revoked when the environment variable changes.
    """

    __tablename__ = "admins"

    #: Telegram user IDs are 64-bit and already exceed the 32-bit range, so this
    #: must be BIGINT. `autoincrement=False` stops SQLAlchemy from treating a
    #: supplied key as one it should generate.
    user_id: Mapped[int] = mapped_column(TelegramId, primary_key=True, autoincrement=False)
    role: Mapped[AdminRole] = mapped_column(
        enum_type(AdminRole, name="admin_role"),
        default=AdminRole.ADMIN,
        server_default=AdminRole.ADMIN.value,
    )
    #: Last known @username / display name, refreshed from incoming updates.
    #: Denormalised on purpose so admin listings need no Telegram round-trip.
    username: Mapped[str | None] = mapped_column(sa.Text)
    display_name: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, default=True, server_default=sa.true()
    )
    #: `SET NULL`, not `CASCADE`: if an admin row ever is removed, the rows they
    #: granted should outlive them rather than disappear.
    added_by: Mapped[int | None] = mapped_column(
        TelegramId, sa.ForeignKey("admins.user_id", ondelete="SET NULL")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        # Both the startup sync and the hot-path permission check filter on
        # these together, and `is_active` alone is not selective enough.
        sa.Index("ix_admins_is_active_role", "is_active", "role"),
        # PostgreSQL does not index a foreign key automatically. `added_by` is
        # nulled out when a granter is removed, which is a lookup on this column.
        sa.Index("ix_admins_added_by", "added_by"),
    )


class Job(Base, TimestampMixin, UuidPrimaryKeyMixin):
    """A unit of durable work claimed by the worker process.

    This is the queue that lets the bot and the worker run as separate
    containers against one database. A job is never held in memory: the worker
    claims a row, renews nothing, and a row whose `locked_at` is older than
    `Settings.JOB_LOCK_TIMEOUT` is presumed crashed and returned to `PENDING`.

    Retries do not create new rows. A failed attempt increments `attempts` and
    pushes `run_after` forward using the equal-jitter backoff in
    `app.utils.retry.compute_backoff`, so the row's identity — and therefore the
    `correlation_id` every log line is tagged with — survives the whole chain.
    """

    __tablename__ = "jobs"

    #: Defaults to the job's own `id` when the caller does not supply one, so a
    #: single piece of work is greppable across processes:
    #: `grep '"correlation_id":"<uuid>"' logs/app.log`.
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, default=uuid.uuid4, index=True
    )
    type: Mapped[JobType] = mapped_column(enum_type(JobType, name="job_type"))
    status: Mapped[JobStatus] = mapped_column(
        enum_type(JobStatus, name="job_status"),
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
    )
    #: Higher runs first. Lets an admin-triggered publish jump ahead of
    #: background refreshes without a second queue.
    priority: Mapped[int] = mapped_column(
        sa.SmallInteger, default=0, server_default=sa.text("0")
    )
    #: Job-type-specific arguments (tweet id, submission id, requested options).
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(
        sa.SmallInteger, default=DEFAULT_MAX_ATTEMPTS, server_default=sa.text("5")
    )
    #: Not claimable before this timestamp. The backoff gate: set to now() on
    #: enqueue, and to `retry.next_attempt_at(...)` after a failed attempt.
    run_after: Mapped[datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    locked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: Identity of the claiming worker (hostname + pid) — purely for operators
    #: diagnosing a stuck job.
    locked_by: Mapped[str | None] = mapped_column(sa.Text)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    requested_by: Mapped[int | None] = mapped_column(
        TelegramId, sa.ForeignKey("admins.user_id", ondelete="SET NULL")
    )
    #: The *staff* chat to report back to, not the channel.
    chat_id: Mapped[int | None] = mapped_column(TelegramId)
    #: Collapses repeated enqueues of the same logical work while one is still
    #: in flight. Enforced by the partial unique index below, not by a
    #: check-then-insert, so two concurrent callers cannot both win a race.
    dedupe_key: Mapped[str | None] = mapped_column(sa.Text)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        # The claim query. Partial, so the index holds only the claimable rows
        # and stays small no matter how much history accumulates. PostgreSQL
        # serves the `priority DESC` ordering with a backward index scan; the
        # pending set is tiny, so any residual sort is immaterial.
        sa.Index(
            "ix_jobs_claim",
            "priority",
            "run_after",
            "created_at",
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # Crash reclaim: find rows stuck in RUNNING past the lock timeout.
        sa.Index(
            "ix_jobs_reclaim",
            "locked_at",
            postgresql_where=sa.text("status = 'running'"),
        ),
        sa.Index("ix_jobs_type_status", "type", "status"),
        # The duplicate-enqueue guarantee. NULLs are distinct in a PostgreSQL
        # unique index, so jobs without a dedupe_key are unaffected. FAILED and
        # DEAD are excluded: once a job is terminal, the same work may be
        # enqueued again deliberately.
        sa.Index(
            "ix_jobs_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text("status IN ('pending', 'running')"),
        ),
        # Powers "/jobs" style listings scoped to the admin who asked.
        sa.Index("ix_jobs_requested_by_created_at", "requested_by", "created_at"),
    )


class Tweet(Base, TimestampMixin):
    """A cached provider payload for one tweet.

    Written by a `FETCH_TWEET` job so that one fetch can serve many publishes
    and a retry never re-hits the provider. `expires_at` is
    `fetched_at + Settings.TWEET_CACHE_TTL`; a TTL of 0 disables caching, in
    which case rows are simply never written.

    Unavailable tweets (deleted, protected, suspended) are stored too, with
    `unavailable_reason` set — negative caching is what stops a dead link being
    retried against the provider forever.
    """

    __tablename__ = "tweets"

    tweet_id: Mapped[str] = mapped_column(sa.String(TWEET_ID_MAX_DIGITS), primary_key=True)
    #: X usernames are at most 15 characters; `app.utils.urls` enforces the same.
    author_username: Mapped[str | None] = mapped_column(sa.String(15))
    author_name: Mapped[str | None] = mapped_column(sa.Text)
    author_id: Mapped[str | None] = mapped_column(sa.String(TWEET_ID_MAX_DIGITS))
    text: Mapped[str | None] = mapped_column(sa.Text)
    lang: Mapped[str | None] = mapped_column(sa.String(16))
    #: When the tweet was posted, which is not when we fetched it.
    posted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    provider: Mapped[str] = mapped_column(sa.String(32))
    #: The normalised provider response, stored whole. Individual fields are
    #: promoted to columns above only where a query or an index needs them;
    #: everything else (metrics, entities, quoted tweets) stays in here rather
    #: than becoming a column that is written once and never read.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    media_count: Mapped[int] = mapped_column(
        sa.SmallInteger, default=0, server_default=sa.text("0")
    )
    unavailable_reason: Mapped[str | None] = mapped_column(sa.Text)

    media_items: Mapped[list[MediaItem]] = relationship(
        "MediaItem",
        back_populates="tweet",
        cascade="all, delete-orphan",
        # The database already cascades; without this the ORM would load every
        # child just to delete it, defeating the FK's `ON DELETE CASCADE`.
        passive_deletes=True,
        order_by="MediaItem.position",
        lazy="raise_on_sql",
    )


class MediaItem(Base, TimestampMixin, UuidPrimaryKeyMixin):
    """One photo, video or animation attached to a tweet.

    `sha256` exists because reposted threads frequently contain the same file
    twice; hashing lets the uploader send it once. Note that nothing here is
    required to be cached locally — `remote_url` alone is enough to publish,
    and the `cached_*` columns are only populated when
    `Settings.MEDIA_CACHE_ENABLED` is on.
    """

    __tablename__ = "media_items"

    tweet_id: Mapped[str] = mapped_column(
        sa.String(TWEET_ID_MAX_DIGITS),
        sa.ForeignKey("tweets.tweet_id", ondelete="CASCADE"),
    )
    #: 0-based order within the tweet, which is the order Telegram must receive
    #: a media group in.
    position: Mapped[int] = mapped_column(sa.SmallInteger)
    kind: Mapped[MediaKind] = mapped_column(enum_type(MediaKind, name="media_kind"))
    remote_url: Mapped[str] = mapped_column(sa.Text)
    content_type: Mapped[str | None] = mapped_column(sa.String(64))
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    #: Accessibility text. Telegram shows this to screen readers, so it is worth
    #: carrying through from the provider rather than dropping.
    alt_text: Mapped[str | None] = mapped_column(sa.Text)
    sha256: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    cached_path: Mapped[str | None] = mapped_column(sa.Text)
    cached_size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    cached_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    cache_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    tweet: Mapped[Tweet] = relationship(
        "Tweet", back_populates="media_items", lazy="raise_on_sql"
    )

    __table_args__ = (
        # Media group order is positional; two items may not claim one slot.
        sa.UniqueConstraint("tweet_id", "position"),
    )


class Publication(Base, TimestampMixin, UuidPrimaryKeyMixin):
    """A record of what actually reached the channel.

    This is the duplicate-suppression backbone of a relay bot: re-running a job,
    retrying after a crash, or two admins submitting the same link must all
    resolve to one post. That guarantee lives in the partial unique index below,
    so it holds under concurrency rather than only in the happy path.

    `message_ids` is a list because a single publish can legitimately produce
    several messages — a media group is up to ten, and
    `LONG_CAPTION_MODE=text_first` deliberately sends the text and the media as
    two messages.
    """

    __tablename__ = "publications"

    #: `RESTRICT`, not `CASCADE`: a publication is the permanent record of a post
    #: that is live in a public channel, so it must not vanish because the tweet
    #: cache was pruned. The cache pruner skips tweets that are referenced here.
    tweet_id: Mapped[str] = mapped_column(
        sa.String(TWEET_ID_MAX_DIGITS),
        sa.ForeignKey("tweets.tweet_id", ondelete="RESTRICT"),
    )
    channel_id: Mapped[int] = mapped_column(TelegramId)
    #: The first message sent, used to build the public link.
    primary_message_id: Mapped[int | None] = mapped_column(TelegramId)
    message_ids: Mapped[list[int]] = mapped_column(
        ARRAY(sa.BigInteger), default=list, server_default=sa.text("'{}'::bigint[]")
    )
    message_url: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[PublicationStatus] = mapped_column(
        enum_type(PublicationStatus, name="publication_status"),
        default=PublicationStatus.PENDING,
        server_default=PublicationStatus.PENDING.value,
    )
    mode: Mapped[PublicationMode] = mapped_column(
        enum_type(PublicationMode, name="publication_mode")
    )
    #: Which template rendered it, kept because `POST_TEMPLATE` can change
    #: between the post and the question "why does this one look different?".
    template: Mapped[str | None] = mapped_column(sa.String(16))
    caption: Mapped[str | None] = mapped_column(sa.Text)
    caption_length: Mapped[int | None] = mapped_column(sa.Integer)
    media_count: Mapped[int] = mapped_column(
        sa.SmallInteger, default=0, server_default=sa.text("0")
    )
    media_sent: Mapped[int] = mapped_column(
        sa.SmallInteger, default=0, server_default=sa.text("0")
    )
    #: `SET NULL`: losing the job row must not lose the publication record.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="SET NULL")
    )
    published_by: Mapped[int | None] = mapped_column(
        TelegramId, sa.ForeignKey("admins.user_id", ondelete="SET NULL")
    )
    published_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    error: Mapped[str | None] = mapped_column(sa.Text)

    tweet: Mapped[Tweet] = relationship("Tweet", lazy="raise_on_sql")

    __table_args__ = (
        # The duplicate-post guarantee. FAILED is excluded so a genuine retry
        # after a hard failure is possible; DELETED is excluded so a post an
        # operator removed by hand can be republished. Everything else — pending,
        # published, partial — claims the (tweet, channel) pair.
        sa.Index(
            "ix_publications_dedupe",
            "tweet_id",
            "channel_id",
            unique=True,
            postgresql_where=sa.text("status NOT IN ('failed', 'deleted')"),
        ),
        # Recent-posts listings per channel. Scanned backward for DESC ordering.
        sa.Index("ix_publications_channel_id_published_at", "channel_id", "published_at"),
        # Non-partial index on tweet_id. The dedupe index above cannot serve the
        # "has this tweet ever been attempted?" lookup, because its predicate
        # excludes FAILED and DELETED rows — which are exactly the rows a
        # retry decision has to consider.
        sa.Index("ix_publications_tweet_id", "tweet_id"),
        # Foreign keys are not indexed automatically by PostgreSQL.
        sa.Index("ix_publications_job_id", "job_id"),
        sa.Index("ix_publications_published_by", "published_by"),
    )


class Submission(Base, TimestampMixin, UuidPrimaryKeyMixin):
    """An admin's pending "preview -> Publish / Cancel" decision.

    Persisted rather than held in an aiogram FSM because `REDIS_URL` is optional
    and an in-process FSM would lose every pending preview on restart — an admin
    would press Publish on a button that no longer means anything.

    `tweet_id` is intentionally **not** a foreign key to `tweets`: the preview is
    created from the URL before the tweet has necessarily been fetched, so the
    cache row may not exist yet.
    """

    __tablename__ = "submissions"

    admin_user_id: Mapped[int] = mapped_column(
        TelegramId, sa.ForeignKey("admins.user_id", ondelete="CASCADE")
    )
    #: The staff chat the preview was posted in — not the channel.
    chat_id: Mapped[int] = mapped_column(TelegramId)
    tweet_id: Mapped[str] = mapped_column(sa.String(TWEET_ID_MAX_DIGITS))
    #: So the preview can be edited into its final state or cleaned up when the
    #: submission expires.
    preview_message_id: Mapped[int | None] = mapped_column(TelegramId)
    status: Mapped[SubmissionStatus] = mapped_column(
        enum_type(SubmissionStatus, name="submission_status"),
        default=SubmissionStatus.PENDING,
        server_default=SubmissionStatus.PENDING.value,
    )
    #: Per-submission overrides (template, media handling) chosen in the UI.
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        # One live preview per admin per tweet: a double-tap must not create two
        # competing confirmations. Terminal states are excluded so the same link
        # can be submitted again later.
        sa.Index(
            "ix_submissions_pending",
            "admin_user_id",
            "tweet_id",
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
        ),
        sa.Index("ix_submissions_admin_user_id_status", "admin_user_id", "status"),
        sa.Index("ix_submissions_chat_id_created_at", "chat_id", "created_at"),
        # Foreign keys are not indexed automatically by PostgreSQL.
        sa.Index("ix_submissions_job_id", "job_id"),
    )


class PublishThrottle(Base, TimestampMixin):
    """Cross-process publish cooldown and channel rate window.

    Telegram allows roughly `constants.CHANNEL_MESSAGES_PER_MINUTE` messages per
    minute to one channel. With the bot and the worker running as separate
    containers, an in-process timer cannot enforce that, and Redis may be
    disabled — so the gate is a single row per scope that both processes contend
    for with `SELECT ... FOR UPDATE`.

    `scope` is a free-form key (`channel:-100123`) rather than a channel id so
    the same mechanism can throttle a second channel, or a per-admin reply
    budget, without a schema change.
    """

    __tablename__ = "publish_throttle"

    scope: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    last_published_at: Mapped[datetime] = mapped_column(TZDateTime)
    #: Start of the current fixed window, with `window_count` messages sent in
    #: it — a rolling count would need one row per message.
    window_started_at: Mapped[datetime] = mapped_column(TZDateTime)
    window_count: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))


class DailyStat(Base, TimestampMixin):
    """Per-day counters, bucketed by the operator's local midnight.

    `day` comes from `app.utils.timeutil.start_of_day(Settings.TIMEZONE)`. That
    is worth stating plainly: changing `TIMEZONE` re-buckets *future* rows only,
    so the stored days do not line up across the change. Existing rows keep
    whatever day they were written under, which is the honest behaviour for a
    counter that has already been published to a human.

    Counters are maintained by upsert on publish, and `skipped_count` tracks
    duplicate rejections — the number that tells an operator the dedupe index is
    doing its job rather than that the bot is broken.
    """

    __tablename__ = "stats_daily"

    day: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    channel_id: Mapped[int] = mapped_column(TelegramId, primary_key=True)
    published_count: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    failed_count: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    skipped_count: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    fetched_count: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    first_published_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_published_at: Mapped[datetime | None] = mapped_column(TZDateTime)
