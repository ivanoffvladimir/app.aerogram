"""Репозиторий трекинга. Единственное место с SQL в модуле."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.tracking.models import DeliveryOutcome, ShipmentEvent

__all__ = ["TrackingRepository"]


class TrackingRepository:
    """Лента событий и факт доставки.

    Тенант в условиях не указывается: его обеспечивает RLS.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def timeline(self, shipment_id: UUID) -> list[ShipmentEvent]:
        """Лента отправления по времени события.

        Порядок — по ``occurred_at``, а не по времени получения: перевозчики
        регулярно отдают события с задержкой и не по порядку, и лента,
        отсортированная по получению, показала бы доставку раньше отправки.
        """
        stmt = (
            select(ShipmentEvent)
            .where(ShipmentEvent.shipment_id == shipment_id)
            .order_by(ShipmentEvent.occurred_at, ShipmentEvent.received_at)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def known_keys(self, shipment_id: UUID) -> set[str]:
        """Отпечатки уже сохранённых событий — защита от дублей.

        Одно и то же событие приходит и вебхуком, и опросом; уникальный индекс
        не даст записать дубль, но выяснять это исключением на каждой пачке
        значило бы откатывать всю пачку из-за одного повторного события.
        """
        stmt = select(ShipmentEvent.dedup_key).where(ShipmentEvent.shipment_id == shipment_id)
        return set((await self._session.execute(stmt)).scalars())

    def add_events(self, events: list[ShipmentEvent]) -> None:
        self._session.add_all(events)

    async def outcome(self, shipment_id: UUID) -> DeliveryOutcome | None:
        return await self._session.get(DeliveryOutcome, shipment_id)

    def add_outcome(self, outcome: DeliveryOutcome) -> DeliveryOutcome:
        self._session.add(outcome)
        return outcome
