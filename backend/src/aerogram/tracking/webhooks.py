"""Подписки тенанта и доставка исходящих вебхуков (FR-3.6).

Доставка отделена от постановки в очередь намеренно. Событие ставится в очередь
в той же транзакции, что и изменение отправления: иначе сбой отправки откатил бы
приём события, и статус, который перевозчик уже сообщил, был бы потерян ради
уведомления. Отправляет очередь фоновая задача — она же и повторяет.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.config import Settings
from aerogram.shared.clock import utcnow
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.errors import NotFound, ValidationFailed
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shipments.models import Shipment
from aerogram.tracking.models import WebhookDelivery, WebhookSubscription
from aerogram.tracking.outgoing import (
    WEBHOOK_EVENTS,
    accepted,
    deliver,
    generate_secret,
    validate_url,
)
from aerogram.tracking.repository import WebhookRepository

__all__ = ["MAX_ATTEMPTS", "RETRY_BASE", "WebhookService", "next_attempt_after"]

log = get_logger(__name__)

#: Пять попыток (FR-3.6). Дальше доставка считается несостоявшейся: получатель,
#: молчащий полтора часа, не оживёт от шестой попытки, а очередь он занимает.
MAX_ATTEMPTS = 5

#: Основание экспоненциальной задержки: 1, 5, 25, 125 минут.
RETRY_BASE = timedelta(minutes=1)
RETRY_FACTOR = 5


def next_attempt_after(attempt: int) -> timedelta | None:
    """Через сколько повторять после ``attempt``-й неудачи.

    ``None`` — попытки исчерпаны. Растёт быстро: получатель, лежащий минуту,
    и получатель, лежащий два часа, — разные ситуации, и частить во втором
    случае значит мешать ему подниматься.
    """
    if attempt >= MAX_ATTEMPTS:
        return None
    return RETRY_BASE * int(RETRY_FACTOR ** (attempt - 1))


class WebhookService:
    """Подписки и очередь доставок тенанта."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._webhooks = WebhookRepository(session)

    def _cipher(self) -> CredentialCipher:
        return CredentialCipher(
            self._settings.credential_key_map, self._settings.credential_active_key_id
        )

    # --- Подписки ---------------------------------------------------------

    async def subscribe(
        self, *, tenant_id: UUID, url: str, events: list[str]
    ) -> tuple[WebhookSubscription, str]:
        """Подписать тенанта. Секрет возвращается ОДИН раз, как API-ключ.

        Он нужен получателю, чтобы проверять подпись, и хранится у нас
        зашифрованным — восстановить его потом нельзя.
        """
        await validate_url(url)
        unknown = sorted(set(events) - WEBHOOK_EVENTS)
        if unknown:
            # Молча проглотить неизвестное событие значит пообещать доставку,
            # которой не будет, и обнаружится это в момент инцидента.
            raise ValidationFailed("Неизвестные события: " + ", ".join(unknown), field="events")
        if not events:
            raise ValidationFailed("Подписка без событий ничего не доставит", field="events")

        secret = generate_secret()
        subscription = WebhookSubscription(
            id=uuid7(),
            tenant_id=tenant_id,
            url=url,
            events=sorted(set(events)),
            secret_encrypted="",
            is_active=True,
        )
        self._webhooks.add_subscription(subscription)
        await self._session.flush()
        # Шифротекст привязан к идентификатору строки: перенос его в чужую
        # подписку не расшифруется.
        subscription.secret_encrypted = self._cipher().encrypt(
            secret, aad=str(subscription.id).encode()
        )
        log.info("webhooks.subscribed", subscription_id=str(subscription.id), events=len(events))
        return subscription, secret

    async def subscriptions(self) -> list[WebhookSubscription]:
        return await self._webhooks.subscriptions()

    async def unsubscribe(self, subscription_id: UUID) -> WebhookSubscription:
        """Отключить подписку. Строка остаётся: по ней читается история доставок."""
        subscription = await self._webhooks.get_subscription(subscription_id)
        if subscription is None:
            raise NotFound("Подписка не найдена")
        subscription.is_active = False
        return subscription

    # --- Очередь ----------------------------------------------------------

    async def enqueue(self, shipment: Shipment, event_type: str) -> int:
        """Поставить событие в очередь всем подписанным. Возвращает число доставок.

        Тело собирается здесь и сохраняется целиком: подписка может измениться
        или отключиться, а доставка обязана уйти с тем содержимым, которое
        описывало событие в момент, когда оно произошло.
        """
        payload = _payload(shipment, event_type)
        queued = 0
        for subscription in await self._webhooks.subscriptions(active_only=True):
            if event_type not in subscription.events:
                continue
            self._webhooks.add_delivery(
                WebhookDelivery(
                    id=uuid7(),
                    tenant_id=shipment.tenant_id,
                    subscription_id=subscription.id,
                    event_type=event_type,
                    shipment_id=shipment.id,
                    payload=payload,
                    attempt=0,
                    next_attempt_at=utcnow(),
                )
            )
            queued += 1
        if queued:
            log.info("webhooks.enqueued", event_type=event_type, deliveries=queued)
        return queued

    async def deliver_due(self, limit: int = 100) -> int:
        """Отправить всё, чему пора. Возвращает число принятых получателем."""
        delivered = 0
        for delivery in await self._webhooks.due_deliveries(limit):
            subscription = await self._webhooks.get_subscription(delivery.subscription_id)
            if subscription is None or not subscription.is_active:
                # Подписку отключили, пока доставка ждала очереди. Отправлять
                # нечего и некуда: снимаем с очереди, не считая это ошибкой.
                delivery.next_attempt_at = None
                delivery.error = "подписка отключена"
                continue
            if await self._attempt(delivery, subscription):
                delivered += 1
        return delivered

    async def _attempt(self, delivery: WebhookDelivery, subscription: WebhookSubscription) -> bool:
        """Одна попытка доставки. Возвращает, приняли ли её."""
        delivery.attempt += 1
        secret = self._cipher().decrypt(
            subscription.secret_encrypted, aad=str(subscription.id).encode()
        )
        try:
            status = await deliver(subscription.url, secret, delivery.event_type, delivery.payload)
        except Exception as exc:
            # Тип, а не текст: в сообщении может оказаться адрес получателя
            # вместе с параметрами, а это уже его данные.
            return self._failed(delivery, subscription, None, type(exc).__name__)

        if not accepted(status):
            return self._failed(delivery, subscription, status, f"код ответа {status}")

        delivery.http_status = status
        delivery.delivered_at = utcnow()
        delivery.next_attempt_at = None
        delivery.error = None
        subscription.last_success_at = utcnow()
        subscription.consecutive_failures = 0
        log.info("webhooks.delivered", event_type=delivery.event_type, attempt=delivery.attempt)
        return True

    def _failed(
        self,
        delivery: WebhookDelivery,
        subscription: WebhookSubscription,
        status: int | None,
        error: str,
    ) -> bool:
        delivery.http_status = status
        delivery.error = error
        wait = next_attempt_after(delivery.attempt)
        delivery.next_attempt_at = None if wait is None else utcnow() + wait
        subscription.last_failure_at = utcnow()
        subscription.consecutive_failures += 1
        log.warning(
            "webhooks.failed",
            event_type=delivery.event_type,
            attempt=delivery.attempt,
            giving_up=wait is None,
            error=error,
        )
        return False


def _payload(shipment: Shipment, event_type: str) -> dict[str, Any]:
    """Тело события.

    Персональных данных здесь нет намеренно: адреса, телефоны и имена
    в исходящее уведомление не попадают (CLAUDE.md §6). Получатель знает
    свой заказ по номеру — этого достаточно, чтобы найти остальное у себя.
    """
    return {
        "event": event_type,
        "occurred_at": utcnow().isoformat(),
        "shipment": {
            "id": str(shipment.id),
            "number": shipment.number,
            "status": str(shipment.status),
            "carrier_status": shipment.carrier_status_raw,
            "tracking_number": shipment.tracking_number,
            "external_id": shipment.external_id,
        },
    }
