"""Репозитории справочников. Единственное место с SQL в модуле (CLAUDE.md §4).

Таблицы справочников платформенные: ``tenant_id`` в них нет, RLS не действует.
Это осознанно — города, терминалы и сопоставления общие для всех тенантов.
Отсюда же следует запрет писать в них что-либо, полученное из адреса клиента:
см. ``normalization.city_full_name``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.directories.models import (
    Carrier,
    CarrierTerminal,
    City,
    CityCarrierMap,
    CityMappingQueue,
)
from aerogram.shared.ids import uuid7

__all__ = [
    "CarrierRepository",
    "CityMappingRepository",
    "CityRepository",
    "TerminalRepository",
]


class CityRepository:
    """Справочник городов ФИАС."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_fias(self, fias_id: str) -> City | None:
        stmt = select(City).where(City.fias_id == fias_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def search(self, query: str, limit: int = 10) -> list[City]:
        """Поиск города по названию в локальном справочнике.

        Работает без обращения к ДаData — именно этот путь остаётся рабочим,
        когда внешний сервис недоступен или исчерпана суточная квота.
        """
        pattern = f"%{query.strip()}%"
        stmt = (
            select(City)
            .where(or_(City.name.ilike(pattern), City.full_name.ilike(pattern)))
            # Сначала совпадения с начала названия: «Ново» должно давать
            # Новосибирск раньше, чем Ивано-Франковск.
            .order_by(
                City.name.ilike(f"{query.strip()}%").desc(),
                City.population.desc().nullslast(),
                City.name,
            )
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def upsert(self, values: dict[str, object]) -> City:
        """Записать город, не затирая уже известные поля пустыми значениями.

        Города приходят из двух источников — стартового справочника и подсказок
        ДаData — и второй знает не всё, что знает первый.
        """
        payload = {k: v for k, v in values.items() if v is not None}
        payload.setdefault("id", uuid7())

        stmt = (
            insert(City)
            .values(**payload)
            .on_conflict_do_update(
                index_elements=[City.fias_id],
                set_={k: v for k, v in payload.items() if k not in ("id", "fias_id")},
            )
            .returning(City)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def count(self) -> int:
        return int(
            (await self._session.execute(select(func.count()).select_from(City))).scalar_one()
        )


class CarrierRepository:
    """Справочник перевозчиков."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_code(self, code: str) -> Carrier | None:
        stmt = select(Carrier).where(Carrier.code == code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> list[Carrier]:
        stmt = select(Carrier).where(Carrier.is_active.is_(True)).order_by(Carrier.name)
        return list((await self._session.execute(stmt)).scalars())


class TerminalRepository:
    """Терминалы и пункты выдачи."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base(self, carrier_id: UUID, *, include_inactive: bool) -> Select[tuple[CarrierTerminal]]:
        stmt = select(CarrierTerminal).where(CarrierTerminal.carrier_id == carrier_id)
        if not include_inactive:
            stmt = stmt.where(CarrierTerminal.is_active.is_(True))
        return stmt

    async def list_in_city(
        self,
        carrier_id: UUID,
        city_fias_id: str,
        *,
        types: tuple[str, ...] = (),
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CarrierTerminal], int]:
        """Терминалы перевозчика в городе.

        Город обязателен: без него запрос выкачивает весь справочник постранично,
        а он измеряется тысячами строк.
        """
        stmt = self._base(carrier_id, include_inactive=False).where(
            CarrierTerminal.city_fias_id == city_fias_id
        )
        if types:
            stmt = stmt.where(CarrierTerminal.type.in_(types))

        total = int(
            (
                await self._session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar_one()
        )
        # Сортировка замыкается на уникальный код: иначе страницы «плывут».
        page = (
            stmt.order_by(CarrierTerminal.type, CarrierTerminal.external_code)
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(page)).scalars()), total

    async def get_by_code(self, carrier_id: UUID, external_code: str) -> CarrierTerminal | None:
        """Терминал по коду, включая погашенный.

        Погашенные нужны: их коды лежат в уже созданных отправлениях, и карточка
        старого заказа обязана остаться читаемой.
        """
        stmt = self._base(carrier_id, include_inactive=True).where(
            CarrierTerminal.external_code == external_code
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class CityMappingRepository:
    """Сопоставление городов с кодами перевозчиков и очередь ручного разбора."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, carrier_id: UUID, city_fias_id: str) -> CityCarrierMap | None:
        stmt = select(CityCarrierMap).where(
            CityCarrierMap.carrier_id == carrier_id,
            CityCarrierMap.city_fias_id == city_fias_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        carrier_id: UUID,
        city_fias_id: str,
        carrier_city_code: str,
        carrier_city_name: str | None,
        match_method: str,
        match_score: float | None,
        is_confirmed: bool,
    ) -> CityCarrierMap:
        """Записать сопоставление, НЕ затирая решение человека.

        Ночная задача, молча меняющая подтверждённый код города, — это ошибка,
        которая обнаруживается по жалобе клиента через сутки. Человеческое
        решение всегда старше машинного.
        """
        stmt = (
            insert(CityCarrierMap)
            .values(
                id=uuid7(),
                carrier_id=carrier_id,
                city_fias_id=city_fias_id,
                carrier_city_code=carrier_city_code,
                carrier_city_name=carrier_city_name,
                match_method=match_method,
                match_score=match_score,
                is_confirmed=is_confirmed,
            )
            .on_conflict_do_update(
                index_elements=[CityCarrierMap.carrier_id, CityCarrierMap.city_fias_id],
                set_={
                    "carrier_city_code": carrier_city_code,
                    "carrier_city_name": carrier_city_name,
                    "match_method": match_method,
                    "match_score": match_score,
                },
                where=CityCarrierMap.is_confirmed.is_(False),
            )
            .returning(CityCarrierMap)
        )
        result = (await self._session.execute(stmt)).scalar_one_or_none()
        if result is not None:
            return result
        # Конфликт с подтверждённой записью: она остаётся как есть.
        existing = await self.resolve(carrier_id, city_fias_id)
        assert existing is not None  # noqa: S101  # запись существует, раз был конфликт
        return existing

    async def enqueue(
        self,
        *,
        carrier_id: UUID,
        carrier_city_code: str,
        carrier_city_name: str | None,
        carrier_region_name: str | None,
        reason: str,
        candidates: list[dict[str, object]],
        best_score: float | None,
        terminals_count: int = 0,
    ) -> CityMappingQueue:
        """Поставить город в очередь ручного сопоставления (FR-12.3)."""
        stmt = (
            insert(CityMappingQueue)
            .values(
                id=uuid7(),
                carrier_id=carrier_id,
                carrier_city_code=carrier_city_code,
                carrier_city_name=carrier_city_name,
                carrier_region_name=carrier_region_name,
                reason=reason,
                candidates=candidates,
                best_score=best_score,
                terminals_count=terminals_count,
            )
            .on_conflict_do_update(
                index_elements=[
                    CityMappingQueue.carrier_id,
                    CityMappingQueue.carrier_city_code,
                ],
                set_={
                    "carrier_city_name": carrier_city_name,
                    "carrier_region_name": carrier_region_name,
                    "reason": reason,
                    "candidates": candidates,
                    "best_score": best_score,
                    "terminals_count": terminals_count,
                },
                where=CityMappingQueue.resolved_at.is_(None),
            )
            .returning(CityMappingQueue)
        )
        result = (await self._session.execute(stmt)).scalar_one_or_none()
        if result is not None:
            return result
        stmt_existing = select(CityMappingQueue).where(
            CityMappingQueue.carrier_id == carrier_id,
            CityMappingQueue.carrier_city_code == carrier_city_code,
        )
        existing = (await self._session.execute(stmt_existing)).scalar_one()
        return existing

    async def list_open(
        self, carrier_id: UUID | None = None, limit: int = 100
    ) -> list[CityMappingQueue]:
        """Неразобранные записи очереди, самые «дорогие» сверху."""
        stmt = select(CityMappingQueue).where(CityMappingQueue.resolved_at.is_(None))
        if carrier_id is not None:
            stmt = stmt.where(CityMappingQueue.carrier_id == carrier_id)
        stmt = stmt.order_by(
            CityMappingQueue.terminals_count.desc(), CityMappingQueue.carrier_city_name
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def get_queue_item(self, item_id: UUID) -> CityMappingQueue | None:
        return await self._session.get(CityMappingQueue, item_id)
