"""The admin roster.

`ADMIN_USER_IDS` is the source of truth for *who may use the bot*. This repository
reconciles the database with it at startup.

Removing an id from the environment **deactivates** that admin rather than
deleting the row. That distinction matters: "revoke access" has to actually
revoke, but the row is also referenced by `publications.published_by` and
`jobs.requested_by`, so deleting it would erase who published what. Deactivation
revokes without losing the audit trail.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.enums import AdminRole
from app.database.models import Admin
from app.utils.timeutil import utcnow


class AdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> Admin | None:
        return await self._session.get(Admin, user_id)

    async def is_active_admin(self, user_id: int) -> bool:
        """The hot-path permission check, run on every incoming update.

        Selects a single column rather than the entity so it never loads a row
        into the identity map just to answer a yes/no question.
        """
        stmt = select(Admin.user_id).where(
            Admin.user_id == user_id, Admin.is_active.is_(True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_active(self) -> list[Admin]:
        stmt = select(Admin).where(Admin.is_active.is_(True)).order_by(Admin.user_id)
        return list((await self._session.execute(stmt)).scalars())

    async def upsert(
        self,
        user_id: int,
        *,
        role: AdminRole | None = None,
        username: str | None = None,
        display_name: str | None = None,
        added_by: int | None = None,
        is_active: bool | None = None,
    ) -> Admin:
        """Insert or update one admin.

        Uses a Core upsert followed by a `get()`, rather than `.returning(Admin)`.
        The single-statement form is shorter but depends on ORM-enabled
        RETURNING for an upsert, and the extra round-trip here happens once per
        admin per startup — not worth the fragility.

        `username`/`display_name` are only overwritten when a new value is given,
        so a sync that does not know them cannot blank what a previous update
        learned.
        """
        values: dict[str, object] = {
            "user_id": user_id,
            "role": role or AdminRole.ADMIN,
            "is_active": True if is_active is None else is_active,
        }
        if username is not None:
            values["username"] = username
        if display_name is not None:
            values["display_name"] = display_name
        if added_by is not None:
            values["added_by"] = added_by

        update_set: dict[str, object] = {
            "role": values["role"],
            "is_active": values["is_active"],
            "updated_at": func.now(),
        }
        if username is not None:
            update_set["username"] = username
        if display_name is not None:
            update_set["display_name"] = display_name

        stmt = (
            pg_insert(Admin)
            .values(**values)
            .on_conflict_do_update(index_elements=[Admin.user_id], set_=update_set)
        )
        await self._session.execute(stmt)
        admin = await self._session.get(Admin, user_id)
        # `get()` can return a stale identity-map instance after a Core upsert,
        # because no ORM-level UPDATE ran. Refresh so callers never see pre-sync
        # values.
        await self._session.refresh(admin) if admin is not None else None
        assert admin is not None  # the upsert just guaranteed the row exists
        return admin

    async def sync_from_settings(self, settings: Settings) -> tuple[list[int], list[int]]:
        """Reconcile the roster with `ADMIN_USER_IDS`.

        Returns `(activated, deactivated)`. The lowest configured id is granted
        `owner` (see `Settings.owner_id`); the rest are `admin`. Ids no longer in
        the environment are deactivated but never deleted.
        """
        configured = sorted(settings.ADMIN_USER_IDS)
        owner_id = settings.owner_id

        for user_id in configured:
            await self.upsert(
                user_id,
                role=AdminRole.OWNER if user_id == owner_id else AdminRole.ADMIN,
                is_active=True,
            )

        stale_stmt = (
            select(Admin.user_id)
            .where(Admin.is_active.is_(True), Admin.user_id.not_in(configured))
        )
        stale = list((await self._session.execute(stale_stmt)).scalars())
        if stale:
            await self._session.execute(
                Admin.__table__.update()
                .where(Admin.user_id.in_(stale))
                .values(is_active=False, updated_at=func.now())
            )
        return configured, stale

    async def touch_last_seen(
        self, user_id: int, *, username: str | None = None, display_name: str | None = None
    ) -> None:
        """Refresh the denormalised profile fields from an incoming update."""
        values: dict[str, object] = {"last_seen_at": utcnow()}
        if username is not None:
            values["username"] = username
        if display_name is not None:
            values["display_name"] = display_name
        await self._session.execute(
            Admin.__table__.update().where(Admin.user_id == user_id).values(**values)
        )

    async def set_active(self, user_id: int, *, active: bool) -> bool:
        result = await self._session.execute(
            Admin.__table__.update()
            .where(Admin.user_id == user_id)
            .values(is_active=active, updated_at=func.now())
        )
        return bool(result.rowcount)
