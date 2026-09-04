"""Единственное место с SQL для массовых отправлений (CLAUDE.md §4).

Все запросы ограничены тенантом. RLS ловит промах на уровне базы, но полагаться
только на неё нельзя: запрос без ``tenant_id`` вернёт пустоту вместо ошибки,
и это выглядит как «данных нет», а не как ошибка в коде.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.bulk.models import BulkRow, BulkRun
from aerogram.shared.enums import BulkRowStatus

__all__ = ["BulkRepository"]


class BulkRepository:
    """Доступ к прогонам и их строкам."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """Сессия — для репозиториев смежных модулей в той же транзакции."""
        return self._session

    def add_run(self, run: BulkRun) -> None:
        self._session.add(run)

    def add_row(self, row: BulkRow) -> None:
        self._session.add(row)

    async def flush(self) -> None:
        """Присвоить идентификаторы и серверные умолчания до чтения.

        Без этого только что созданный прогон читается с пустыми ``id``
        и ``created_at``: они присваиваются при сбросе в базу, а не при
        добавлении в сессию.
        """
        await self._session.flush()

    async def get_run(self, run_id: UUID, *, tenant_id: UUID) -> BulkRun | None:
        stmt = select(BulkRun).where(BulkRun.id == run_id, BulkRun.tenant_id == tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def rows_of(self, run_id: UUID, *, tenant_id: UUID) -> list[BulkRow]:
        stmt = (
            select(BulkRow)
            .where(BulkRow.run_id == run_id, BulkRow.tenant_id == tenant_id)
            .order_by(BulkRow.position)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def page(self, *, tenant_id: UUID, limit: int, offset: int) -> tuple[list[BulkRun], int]:
        """Страница прогонов, новые сверху."""
        stmt = (
            select(BulkRun)
            .where(BulkRun.tenant_id == tenant_id)
            .order_by(BulkRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars())
        total = await self._session.scalar(
            select(func.count()).select_from(BulkRun).where(BulkRun.tenant_id == tenant_id)
        )
        return items, int(total or 0)

    async def counts_by_status(self, run_id: UUID, *, tenant_id: UUID) -> dict[str, int]:
        """Сколько строк в каком состоянии.

        Считается в базе, а не перебором строк: прогон может быть на тысячу
        получателей, и выгружать их ради счётчика незачем.
        """
        stmt = (
            select(BulkRow.status, func.count())
            .where(BulkRow.run_id == run_id, BulkRow.tenant_id == tenant_id)
            .group_by(BulkRow.status)
        )
        rows = (await self._session.execute(stmt)).all()
        return {str(status): int(count) for status, count in rows}

    async def unfinished_count(self, run_id: UUID, *, tenant_id: UUID) -> int:
        """Сколько строк ещё в работе.

        Прогон завершён, когда их ноль, — даже если часть строк не прошла:
        частичный успех нормальное состояние (ADR-0022).
        """
        stmt = (
            select(func.count())
            .select_from(BulkRow)
            .where(
                BulkRow.run_id == run_id,
                BulkRow.tenant_id == tenant_id,
                BulkRow.status.notin_([BulkRowStatus.CREATED, BulkRowStatus.FAILED]),
            )
        )
        return int(await self._session.scalar(stmt) or 0)
