"""Репозиторий трекинга. Единственное место с SQL в модуле."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.shared.clock import utcnow
from aerogram.tracking.models import (
    DeliveryOutcome,
    ShipmentEvent,
    WebhookDelivery,
    WebhookSubscription,
)

__all__ = ["TrackingRepository", "WebhookRepository"]


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


class WebhookRepository:
    """Подписки тенанта и очередь доставок (FR-3.6)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_subscription(self, subscription: WebhookSubscription) -> WebhookSubscription:
        self._session.add(subscription)
        return subscription

    async def get_subscription(self, subscription_id: UUID) -> WebhookSubscription | None:
        return await self._session.get(WebhookSubscription, subscription_id)

    async def subscriptions(self, *, active_only: bool = False) -> list[WebhookSubscription]:
        stmt = select(WebhookSubscription).order_by(WebhookSubscription.created_at)
        if active_only:
            stmt = stmt.where(WebhookSubscription.is_active.is_(True))
        return list((await self._session.execute(stmt)).scalars())

    def add_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        self._session.add(delivery)
        return delivery

    async def due_deliveries(self, limit: int = 100) -> list[WebhookDelivery]:
        """Доставки, которым пора уйти или уйти повторно.

        Доставленные исключены по ``delivered_at``, исчерпавшие попытки —
        по обнулённому ``next_attempt_at``: пустое поле означает «больше
        не пытаемся», и отличить его от «пора сейчас» обязано условие,
        а не порядок строк.
        """
        stmt = (
            select(WebhookDelivery)
            .where(
                WebhookDelivery.delivered_at.is_(None),
                WebhookDelivery.next_attempt_at.is_not(None),
                WebhookDelivery.next_attempt_at <= utcnow(),
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())
