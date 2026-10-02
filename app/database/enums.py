"""Database enums, stored as `VARCHAR` plus a named `CHECK` constraint.

Why not a native PostgreSQL `ENUM`? The integrity guarantee is identical, but
native types are a well-known Alembic trap:

* `ALTER TYPE ... ADD VALUE` cannot be used in the same transaction that adds it,
  which breaks the usual "one migration = one transaction" assumption;
* values can never be removed or reordered, so a status that is retired has to
  be left in place forever;
* `alembic` autogenerate does not diff enum members reliably, so a change can
  pass review and never reach the database.

A `VARCHAR` + `CHECK` costs a few bytes per row — irrelevant at this scale — and
turns every future status change into a one-line `drop_constraint`/`check`
migration. `sa.Enum` renders the `CHECK` for us and keeps Python-side validation.

Members are declared as `StrEnum` so that a comparison against a plain string
works, which keeps call sites readable (`job.status == JobStatus.PENDING`) while
still allowing `job.status == "pending"`.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SaEnum


class AdminRole(StrEnum):
    """Permission level of a Telegram user allowed to drive the bot.

    `OWNER` is granted to `Settings.owner_id` (the lowest configured admin ID)
    at startup; everyone else listed in `ADMIN_USER_IDS` is an `ADMIN`.
    """

    OWNER = "owner"
    ADMIN = "admin"


class JobType(StrEnum):
    """What a queue row asks a worker to do.

    Fetching and publishing are separate job types because they are separately
    throttled (`MAX_CONCURRENT_FETCHES` vs `MAX_CONCURRENT_PUBLISHES`) and a
    fetched tweet is cached, so one fetch can serve many publishes.
    """

    FETCH_TWEET = "fetch_tweet"
    PUBLISH_TWEET = "publish_tweet"
    DOWNLOAD_MEDIA = "download_media"


class JobStatus(StrEnum):
    """Lifecycle of a queue row.

    `PENDING` is the only claimable state: a retry does **not** leave the row in
    a distinct "failed" state, it returns it to `PENDING` with `run_after` pushed
    into the future by the backoff in `app.utils.retry`. The two terminal failure
    states are kept apart because they need different operator responses:

    * `FAILED` — gave up immediately on a non-retryable error (tweet deleted,
      malformed payload). Retrying by hand is pointless.
    * `DEAD` — every attempt was used. Retrying by hand is reasonable.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class PublicationStatus(StrEnum):
    """Outcome of an attempt to put a tweet into the channel.

    `PARTIAL` is a real state, not padding: with `LONG_CAPTION_MODE=text_first`
    the text message can be delivered and the subsequent media upload can still
    fail. Recording that honestly is what lets an operator decide whether to
    delete the half-post rather than silently double-posting it.
    """

    PENDING = "pending"
    PUBLISHED = "published"
    PARTIAL = "partial"
    FAILED = "failed"
    DELETED = "deleted"


class PublicationMode(StrEnum):
    """Whether the post went out automatically or after a human confirmed it."""

    AUTO = "auto"
    MANUAL = "manual"


class SubmissionStatus(StrEnum):
    """Lifecycle of an admin's pending preview.

    These rows exist so that the "preview -> Publish / Cancel" flow survives a
    bot restart without Redis. `REDIS_URL` is optional, so this table is the
    authoritative store rather than a fallback that only runs when Redis is off.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class MediaKind(StrEnum):
    """How the media is sent to Telegram, which drives which Bot API method is used."""

    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"


def enum_type(enum_cls: type[StrEnum], *, name: str) -> SaEnum:
    """Build a `VARCHAR`-backed `Enum` column type with a named `CHECK`.

    `values_callable` is essential: by default SQLAlchemy persists the member
    *names* (`"PENDING"`), not their values (`"pending"`). Storing the upper-case
    names would make the database inconsistent with every configuration value and
    API payload in the project, so the values are pinned here explicitly.

    `validate_strings` raises in Python before the statement reaches PostgreSQL,
    which turns a typo into a clear traceback instead of an `IntegrityError`.

    `length` is set explicitly rather than left to SQLAlchemy's inference. The
    initial migration declares these columns as a literal `VARCHAR(n)`, so an
    inferred length would make the two drift silently the first time a longer
    member is added — the migration would keep the old width and the failure
    would only appear as a truncation error in production.
    """
    return SaEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=max(len(member.value) for member in enum_cls),
        values_callable=lambda cls: [member.value for member in cls],
    )
