"""Репозиторий отправлений. Единственное место с SQL в модуле."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.shared.clock import utcnow
from aerogram.shared.enums import ShipmentStatus
from aerogram.shipments.models import Shipment

__all__ = ["ShipmentRepository"]


class ShipmentRepository:
    """Отправления тенанта.

    Тенант в условиях не указывается: его обеспечивает RLS. Явный фильтр
    дублировал бы политику и создавал ложное впечатление, что без него утечёт.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, shipment: Shipment) -> Shipment:
        self._session.add(shipment)
        return shipment

    async def get(self, shipment_id: UUID) -> Shipment | None:
        return await self._session.get(Shipment, shipment_id)

    async def by_idempotency_key(self, key: str) -> Shipment | None:
        stmt = select(Shipment).where(Shipment.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def by_decision(self, decision_id: UUID) -> Shipment | None:
        """Отправление, созданное по этому решению.

        Одно решение — одно отправление: повторное создание по тому же
        выбору означало бы два заказа у перевозчика на один груз.
        """
        stmt = select(Shipment).where(Shipment.decision_id == decision_id)
        return (await self._session.execute(stmt)).scalars().first()

    async def by_number(self, number: str) -> Shipment | None:
        stmt = select(Shipment).where(Shipment.number == number)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def unconfirmed(self, limit: int = 100) -> list[Shipment]:
        """Черновики, которые перевозчик не подтвердил.

        Это и есть кандидаты в «призраки» (FR-2.5): номер выдан, запрос
        мог уйти, а ответ не дошёл. Сначала самые старые — у них шанс
        оказаться настоящим заказом выше.
        """
        stmt = (
            select(Shipment)
            .where(Shipment.status == ShipmentStatus.DRAFT, Shipment.external_id.is_(None))
            .order_by(Shipment.created_at)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def due_for_poll(self, limit: int = 200) -> list[Shipment]:
        """Отправления, которым пора опросить статус (FR-3.2).

        Черновики исключены: у них нет заказа у перевозчика, спрашивать не о чем.
        Сначала те, кого дольше всех не опрашивали, — иначе при нехватке лимита
        одни и те же отправления обновлялись бы всегда, а другие никогда.
        """
        stmt = (
            select(Shipment)
            .where(
                Shipment.external_id.is_not(None),
                Shipment.next_poll_at.is_not(None),
                Shipment.next_poll_at <= utcnow(),
            )
            .order_by(Shipment.next_poll_at)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    def _filtered(
        self, status: str | None, carrier_id: UUID | None, q: str | None
    ) -> Select[tuple[Shipment]]:
        stmt = select(Shipment)
        if status is not None:
            stmt = stmt.where(Shipment.status == status)
        if carrier_id is not None:
            stmt = stmt.where(Shipment.carrier_id == carrier_id)
        if q:
            # Поиск оператора: он держит в руках либо наш номер, либо трек ТК.
            pattern = f"%{q}%"
            stmt = stmt.where(
                Shipment.number.ilike(pattern)
                | Shipment.tracking_number.ilike(pattern)
                | Shipment.external_id.ilike(pattern)
            )
        return stmt

    async def page(
        self,
        *,
        status: str | None = None,
        carrier_id: UUID | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[Shipment], int]:
        """Страница списка и общее число подходящих строк."""
        stmt = self._filtered(status, carrier_id, q)
        total = (
            await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            await self._session.execute(
                stmt.order_by(Shipment.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        return list(rows), total
