"""Declarative base, metadata conventions and shared column mixins.

The `MetaData` naming convention below is not cosmetic. Without it PostgreSQL
invents its own constraint names, which means:

* `alembic` autogenerate cannot tell a rename from a drop-and-create, so it
  emits churn;
* `op.drop_constraint()` needs a name up front and has nothing to use;
* constraint names differ between a database created by a migration and one
  created by `create_all()`, so tests would not catch real drift.

Declaring it once here makes every name deterministic and reproducible.

Nullability is inferred from the annotation — `Mapped[str]` is `NOT NULL` and
`Mapped[str | None]` is nullable — so it is deliberately not repeated on every
column. Types that need to be explicit for PostgreSQL (sizes, `BIGINT`) are
spelled out.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Applied to every table through `Base.metadata`.
#:
#: `column_0_N_name` concatenates all constrained columns, which is what keeps
#: composite index and unique-constraint names unique. Keep an eye on
#: PostgreSQL's 63-byte identifier limit: pass an explicit `name=` to
#: `Index`/`UniqueConstraint` when the generated one would be truncated.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Every datetime column in this schema is timezone-aware and holds UTC.
#: See `app.utils.timeutil` — only *display* converts to the configured zone.
#: A single shared type instance avoids the classic bug where one column is
#: declared `DateTime` without `timezone=True` and silently loses its offset.
TZDateTime = DateTime(timezone=True)


class Base(DeclarativeBase):
    """Common declarative base for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """Identify a row by its primary key — never by a column that may hold
        user content, so an accidental log of an object cannot leak tweet text."""
        pk = [
            f"{column.name}={getattr(self, column.name, None)!r}"
            for column in self.__table__.primary_key.columns
        ]
        return f"<{type(self).__name__} {' '.join(pk)}>"


class TimestampMixin:
    """`created_at` / `updated_at`, maintained by the database.

    Server-side defaults are deliberate: rows inserted by a migration, by `psql`
    or by a future service still get correct timestamps, and nothing depends on
    the application remembering to set them.

    `onupdate` is client-side SQLAlchemy: it is applied to ORM flushes and Core
    `update()` statements, but **not** to raw `text()` SQL. Every write path in
    this project goes through SQLAlchemy, so that is sufficient — but a manual
    `UPDATE` in psql must set `updated_at` itself.
    """

    #: Deliberately *not* indexed here. An index on every table's `created_at`
    #: would be eight mostly-unused indexes; the queries that need one declare a
    #: composite index that includes it (see `Job.__table_args__`).
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )


class UuidPrimaryKeyMixin:
    """A client-generated UUID primary key.

    Generated in Python rather than by `gen_random_uuid()` so the identifier is
    known *before* the `INSERT`. That matters for `jobs`: the repository can bind
    the job's id as the `correlation_id` in `app.logging_config` and every log
    line of the job's lifetime is already tagged by the time the row is written.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, autoincrement=False
    )


#: Telegram identifiers (user, chat and message ids) are 64-bit and already
#: exceed `INTEGER`. Re-exported so models do not each import `BigInteger`.
TelegramId = BigInteger
