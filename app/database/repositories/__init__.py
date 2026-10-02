"""Repositories: one module per aggregate, each wrapping an `AsyncSession`.

A repository takes a session in its constructor and never creates one, commits,
or rolls back — transaction boundaries belong to the caller (`session_scope()` in
`app.database.session`). That is what lets one HTTP-ish unit of work span several
repositories atomically: reserving a publication, enqueuing its job and bumping a
statistic either all land or none do.

The alternative — self-committing repositories — reads well until the first
operation that needs two of them.
"""

from __future__ import annotations

from app.database.repositories.admins import AdminRepository
from app.database.repositories.jobs import JobRepository
from app.database.repositories.publications import PublicationRepository
from app.database.repositories.stats import StatsRepository
from app.database.repositories.submissions import SubmissionRepository
from app.database.repositories.throttle import ThrottleRepository
from app.database.repositories.tweets import TweetRepository

__all__ = [
    "AdminRepository",
    "JobRepository",
    "PublicationRepository",
    "StatsRepository",
    "SubmissionRepository",
    "ThrottleRepository",
    "TweetRepository",
]
