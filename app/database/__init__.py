"""Database models, session management and repositories.

The model classes are re-exported here for convenience (`from app.database
import Job`), but the re-export has a second, more important effect: importing
the package registers every table on `Base.metadata`. Alembic autogenerate only
sees models that have actually been imported, so a forgotten import would
silently produce an empty or partial migration rather than an error. Funnelling
them through the package makes that mistake impossible.
"""

from __future__ import annotations

from app.database.base import Base, TelegramId, TimestampMixin, TZDateTime, UuidPrimaryKeyMixin
from app.database.enums import (
    AdminRole,
    JobStatus,
    JobType,
    MediaKind,
    PublicationMode,
    PublicationStatus,
    SubmissionStatus,
)
from app.database.models import (
    Admin,
    DailyStat,
    Job,
    MediaItem,
    Publication,
    PublishThrottle,
    Submission,
    Tweet,
)
from app.database.session import (
    DatabaseHealth,
    check_database_connection,
    dispose_engine,
    get_engine,
    get_session,
    get_sessionmaker,
    session_scope,
)

__all__ = [
    "Admin",
    "AdminRole",
    "Base",
    "DailyStat",
    "DatabaseHealth",
    "Job",
    "JobStatus",
    "JobType",
    "MediaItem",
    "MediaKind",
    "Publication",
    "PublicationMode",
    "PublicationStatus",
    "PublishThrottle",
    "Submission",
    "SubmissionStatus",
    "TZDateTime",
    "TelegramId",
    "TimestampMixin",
    "Tweet",
    "UuidPrimaryKeyMixin",
    "check_database_connection",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "session_scope",
]
