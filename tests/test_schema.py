"""Schema invariants, asserted against `Base.metadata` with no database.

These tests exist because the failures they catch are invisible until too late.
An unindexed foreign key or a naive datetime column does not break a unit test or
a staging deploy — it shows up as a slow delete, a truncated string or a
mis-ordered post in production, months later. Asserting the invariants directly
on the metadata costs nothing and runs in milliseconds.

They also guard the hand-written initial migration: the enum widths and the
partial-index predicates are declared literally there, so a change to the models
that is not reflected in the migration fails here first.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

# Importing the package registers every table; the import is the point.
from app.database import models  # noqa: F401
from app.database.base import Base

EXPECTED_TABLES = frozenset(
    {
        "admins",
        "jobs",
        "tweets",
        "media_items",
        "publications",
        "submissions",
        "publish_throttle",
        "stats_daily",
    }
)

_CONSTRAINT_PREFIXES = {
    "primary_key_constraint": "pk_",
    "foreign_key_constraint": "fk_",
    "unique_constraint": "uq_",
    "check_constraint": "ck_",
}

#: Columns that hold a Telegram identifier. All of these already exceed the
#: 32-bit range, so `INTEGER` would overflow — `BIGINT` is mandatory, not a
#: preference. Spelled out rather than inferred so a new id column is a
#: deliberate addition to this list.
_TELEGRAM_ID_COLUMNS = (
    ("admins", "user_id"),
    ("admins", "added_by"),
    ("jobs", "requested_by"),
    ("jobs", "chat_id"),
    ("publications", "channel_id"),
    ("publications", "primary_message_id"),
    ("publications", "published_by"),
    ("submissions", "admin_user_id"),
    ("submissions", "chat_id"),
    ("submissions", "preview_message_id"),
    ("stats_daily", "channel_id"),
)


def _partial_where(index: sa.Index) -> str:
    """The index's `WHERE` clause as SQL text, or `""` when it is not partial."""
    try:
        where = index.dialect_options["postgresql"].get("where")
    except (AttributeError, KeyError):
        return ""
    return str(where) if where is not None else ""


def _leading_index_columns(table: sa.Table) -> set[str]:
    """Columns that lead a non-partial index, unique constraint or primary key.

    Partial indexes are excluded on purpose: a predicate means the index cannot
    serve a general lookup, so it does not stand in for a foreign key index.
    """
    leading: set[str] = set()
    for index in table.indexes:
        columns = list(index.columns)
        if columns and not _partial_where(index):
            leading.add(columns[0].name)
    for constraint in table.constraints:
        if isinstance(constraint, (sa.PrimaryKeyConstraint, sa.UniqueConstraint)):
            columns = list(constraint.columns)
            if columns:
                leading.add(columns[0].name)
    return leading


def test_importing_the_package_registers_every_table() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_table_dependencies_have_no_cycles() -> None:
    """`sorted_tables` raises on a cycle; the ordering assertions pin creation order."""
    ordered = [table.name for table in Base.metadata.sorted_tables]
    assert set(ordered) == EXPECTED_TABLES
    assert ordered.index("admins") < ordered.index("jobs")
    assert ordered.index("tweets") < ordered.index("media_items")
    assert ordered.index("tweets") < ordered.index("publications")
    assert ordered.index("jobs") < ordered.index("publications")
    assert ordered.index("admins") < ordered.index("submissions")
    assert ordered.index("jobs") < ordered.index("submissions")


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_constraint_names_follow_the_naming_convention(table_name: str) -> None:
    """Unnamed constraints would make migrations unable to drop them by name."""
    table = Base.metadata.tables[table_name]
    for constraint in table.constraints:
        prefix = _CONSTRAINT_PREFIXES.get(constraint.__visit_name__)
        assert prefix is not None, f"{table_name}: unexpected {constraint.__visit_name__}"
        # Accessing `.name` is what triggers the naming convention.
        assert constraint.name, f"{table_name}: a {prefix}* constraint has no name"
        assert constraint.name.startswith(prefix), (
            f"{table_name}: {constraint.name!r} does not start with {prefix!r}"
        )


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_foreign_key_columns_are_indexed(table_name: str) -> None:
    """PostgreSQL does not index foreign keys automatically.

    An unindexed foreign key turns every parent delete or primary-key update into
    a sequential scan of the child table.
    """
    table = Base.metadata.tables[table_name]
    covered = _leading_index_columns(table)
    uncovered = sorted({fk.parent.name for fk in table.foreign_keys} - covered)
    assert not uncovered, f"{table_name}: foreign keys without an index: {uncovered}"


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_every_table_is_timestamped(table_name: str) -> None:
    columns = set(Base.metadata.tables[table_name].columns.keys())
    assert {"created_at", "updated_at"} <= columns


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_datetime_columns_are_timezone_aware(table_name: str) -> None:
    """A naive timestamp silently loses the offset and mis-orders posts.

    Everything is stored in UTC (`app.utils.timeutil`); only display converts to
    the configured zone, so `timezone=True` is required on every datetime column.
    """
    table = Base.metadata.tables[table_name]
    naive = [
        column.name
        for column in table.columns
        if isinstance(column.type, sa.DateTime) and not column.type.timezone
    ]
    assert not naive, f"{table_name}: timezone-naive datetime columns: {naive}"


@pytest.mark.parametrize(("table_name", "column_name"), _TELEGRAM_ID_COLUMNS)
def test_telegram_identifiers_are_bigint(table_name: str, column_name: str) -> None:
    column = Base.metadata.tables[table_name].c[column_name]
    assert isinstance(column.type, sa.BigInteger), (
        f"{table_name}.{column_name} is {column.type!r}; Telegram ids need BIGINT"
    )


def test_publication_dedupe_index_is_partial_unique() -> None:
    """The duplicate-post guarantee for the channel.

    A plain unique constraint would forbid a legitimate retry after a hard
    failure, so the predicate is load-bearing and is asserted here.
    """
    table = Base.metadata.tables["publications"]
    index = next(i for i in table.indexes if i.name == "ix_publications_dedupe")
    assert index.unique
    assert [column.name for column in index.columns] == ["tweet_id", "channel_id"]
    where = _partial_where(index)
    assert "failed" in where and "deleted" in where


def test_job_dedupe_index_excludes_terminal_states() -> None:
    """A duplicate enqueue is rejected only while the original is still in flight."""
    table = Base.metadata.tables["jobs"]
    index = next(i for i in table.indexes if i.name == "ix_jobs_dedupe_key")
    assert index.unique
    assert [column.name for column in index.columns] == ["dedupe_key"]
    where = _partial_where(index)
    assert "pending" in where and "running" in where


def test_job_claim_index_is_partial_on_pending() -> None:
    """The worker's claim query must not scan completed history."""
    table = Base.metadata.tables["jobs"]
    index = next(i for i in table.indexes if i.name == "ix_jobs_claim")
    assert "pending" in _partial_where(index)
    assert [column.name for column in index.columns] == ["priority", "run_after", "created_at"]


def test_submission_preview_index_is_partial_unique_on_pending() -> None:
    """One live preview per admin per tweet: a double-tap must not create two."""
    table = Base.metadata.tables["submissions"]
    index = next(i for i in table.indexes if i.name == "ix_submissions_pending")
    assert index.unique
    assert "pending" in _partial_where(index)


def test_enums_persist_values_not_member_names() -> None:
    """Guards the `values_callable` decision in `app.database.enums`.

    SQLAlchemy defaults to storing member *names*, which would put `"PENDING"` in
    the database while every configuration value and API payload in the project
    says `"pending"`.
    """
    column = Base.metadata.tables["jobs"].c.status
    assert list(column.type.enums) == [
        "pending",
        "running",
        "succeeded",
        "failed",
        "dead",
        "cancelled",
    ]


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_no_native_postgres_enum_types(table_name: str) -> None:
    """Native `ENUM` makes `ALTER TYPE ADD VALUE` a migration footgun."""
    table = Base.metadata.tables[table_name]
    for column in table.columns:
        if isinstance(column.type, sa.Enum):
            assert column.type.native_enum is False, f"{table_name}.{column.name}"


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_enum_columns_fit_their_longest_member(table_name: str) -> None:
    """The migration declares these as a literal `VARCHAR(n)`.

    If a longer member is added without widening both, this fails here rather
    than as a truncation error against a live database.
    """
    table = Base.metadata.tables[table_name]
    for column in table.columns:
        if not isinstance(column.type, sa.Enum) or column.type.native_enum:
            continue
        longest = max(len(value) for value in column.type.enums)
        assert column.type.length == longest, (
            f"{table_name}.{column.name}: VARCHAR({column.type.length}) but the "
            f"longest member is {longest} characters"
        )
