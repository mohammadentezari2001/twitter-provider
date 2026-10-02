"""Daily rollups for `/stats`.

Two details are easy to get wrong and are handled here rather than at the call
site:

* **The day is the operator's local day.** `timeutil.start_of_day()` returns the
  *instants* of local midnight in UTC, which is what ranges need — but the DATE
  column must hold the local calendar date. In a UTC+3:30 timezone, local midnight
  on the 1st is 20:30 UTC on the 30th, so calling `.date()` on the result of
  `start_of_day()` would file a day's traffic under the wrong heading.
  `local_day()` converts to the zone first and only then takes the date.
* **Counters accumulate with `+ excluded`, not `= excluded`.** Two publishes in the
  same second both upsert the same row; assignment would lose one of them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DailyStat
from app.utils.timeutil import to_tz, utcnow


def local_day(tz: ZoneInfo | str, *, now: datetime | None = None) -> date:
    """The calendar date it is *right now* in `tz`."""
    return to_tz(now or utcnow(), tz).date()


class StatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bump(
        self,
        *,
        day: date,
        channel_id: int,
        published: int = 0,
        failed: int = 0,
        skipped: int = 0,
        fetched: int = 0,
        published_at: datetime | None = None,
    ) -> None:
        """Add to today's counters, creating the row on first use.

        `published_at` feeds the first/last timestamps. `least`/`greatest` ignore
        NULLs in PostgreSQL, so the first value wins cleanly without a separate
        `COALESCE` on each side.
        """
        values = {
            "day": day,
            "channel_id": channel_id,
            "published_count": published,
            "failed_count": failed,
            "skipped_count": skipped,
            "fetched_count": fetched,
            "first_published_at": published_at,
            "last_published_at": published_at,
        }
        stmt = pg_insert(DailyStat).values(**values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[DailyStat.day, DailyStat.channel_id],
            set_={
                "published_count": DailyStat.published_count + excluded.published_count,
                "failed_count": DailyStat.failed_count + excluded.failed_count,
                "skipped_count": DailyStat.skipped_count + excluded.skipped_count,
                "fetched_count": DailyStat.fetched_count + excluded.fetched_count,
                "first_published_at": func.least(
                    DailyStat.first_published_at, excluded.first_published_at
                ),
                "last_published_at": func.greatest(
                    DailyStat.last_published_at, excluded.last_published_at
                ),
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def for_day(self, *, day: date, channel_id: int) -> DailyStat | None:
        stmt = select(DailyStat).where(
            DailyStat.day == day, DailyStat.channel_id == channel_id
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def range(
        self, *, since: date, until: date, channel_id: int | None = None
    ) -> list[DailyStat]:
        """Rows for `since <= day <= until`, oldest first."""
        stmt = (
            select(DailyStat)
            .where(DailyStat.day >= since, DailyStat.day <= until)
            .order_by(DailyStat.day)
        )
        if channel_id is not None:
            stmt = stmt.where(DailyStat.channel_id == channel_id)
        return list((await self._session.execute(stmt)).scalars())

    async def totals(
        self, *, since: date, until: date, channel_id: int | None = None
    ) -> dict[str, int]:
        stmt = select(
            func.coalesce(func.sum(DailyStat.published_count), 0),
            func.coalesce(func.sum(DailyStat.failed_count), 0),
            func.coalesce(func.sum(DailyStat.skipped_count), 0),
            func.coalesce(func.sum(DailyStat.fetched_count), 0),
        ).where(DailyStat.day >= since, DailyStat.day <= until)
        if channel_id is not None:
            stmt = stmt.where(DailyStat.channel_id == channel_id)
        published, failed, skipped, fetched = (await self._session.execute(stmt)).one()
        return {
            "published": int(published),
            "failed": int(failed),
            "skipped": int(skipped),
            "fetched": int(fetched),
        }

    async def last_n_days(
        self, *, days: int, tz: ZoneInfo | str, channel_id: int | None = None
    ) -> list[DailyStat]:
        """The trailing `days` window ending today, local time. For `/stats`."""
        today = local_day(tz)
        return await self.range(
            since=today - timedelta(days=max(0, days - 1)),
            until=today,
            channel_id=channel_id,
        )
