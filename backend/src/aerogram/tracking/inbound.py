"""Приём вебхуков от перевозчиков (FR-3.1).

Вебхук приходит **без контекста тенанта**: перевозчик знает только свой
идентификатор заказа. Порядок здесь продиктован этим и решением ADR-0015.

1. Адаптер разбирает тело и говорит, по какому заказу пришло событие.
   Формат знает только он.
2. Отправление ищется узким окном поиска (значение ``webhook``, миграция
   0010) — точечно, по перевозчику и идентификатору заказа. Окно живёт одну
   транзакцию, только на чтение, и открывается не здесь, а в единственном
   месте: ``shipments.repository.find_for_webhook``.
3. Тенант становится известен ИЗ найденного отправления. Дальше работа идёт
   обычной транзакцией с установленным тенантом — то есть под теми же
   правилами RLS, что и любой запрос пользователя.
4. Подпись проверяется секретом ЭТОГО тенанта, до единой записи в базу.

Проверка после поиска, а не до, — следствие того, что одним перевозчиком
пользуются несколько тенантов: пока отправление не найдено, неизвестно, чьим
секретом проверять. Ничего не записывается, пока подпись не сошлась.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from aerogram.carriers import registry
from aerogram.config import Settings
from aerogram.core.repository import CarrierAccountRepository
from aerogram.core.service import decrypt_credentials
from aerogram.db import session_scope
from aerogram.directories.repository import CarrierRepository
from aerogram.shared.enums import EventSource
from aerogram.shared.errors import AuthenticationError, NotFound, ValidationFailed
from aerogram.shared.logging import get_logger
from aerogram.shipments.repository import ShipmentRepository
from aerogram.tracking.service import TrackingService

__all__ = ["CREDENTIAL_FIELD", "InboundWebhookService"]

log = get_logger(__name__)

#: Поле секрета подписи в конверте учётных данных перевозчика. Живёт там же,
#: где остальные доступы: это секрет, и в ``settings`` (JSONB без шифрования)
#: ему не место.
CREDENTIAL_FIELD = "webhook_secret"


class InboundWebhookService:
    """Событие от перевозчика — в ленту отправления."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def accept(
        self, carrier_code: str, payload: dict[str, Any], *, body: bytes, headers: dict[str, str]
    ) -> int:
        """Принять вебхук. Возвращает число новых событий в лентах.

        Ноль означает, что событие уже было или отправление нам неизвестно, —
        и то и другое штатно: перевозчик повторяет доставку, пока не получит
        подтверждения, а заказ мог быть создан не у нас.
        """
        adapter, carrier_id = await self._carrier(carrier_code)
        try:
            updates = adapter.parse_webhook(payload)
        except Exception as exc:
            # Разбор чужого тела — не наша ошибка, но и не повод для 500:
            # текст исключения наружу не отдаётся.
            log.warning("webhook.unparsable", carrier=carrier_code, error_type=type(exc).__name__)
            raise ValidationFailed("Не удалось разобрать вебхук") from None

        accepted = 0
        for update in updates:
            accepted += await self._one(
                adapter,
                carrier_code=carrier_code,
                carrier_id=carrier_id,
                external_id=update.external_id,
                events=list(update.events),
                body=body,
                headers=headers,
            )
        return accepted

    async def _carrier(self, carrier_code: str) -> tuple[Any, UUID]:
        """Перевозчик и его адаптер. Оба обязаны существовать."""
        async with session_scope() as session:
            carrier = await CarrierRepository(session).get_by_code(carrier_code)
            if carrier is None:
                raise NotFound("Перевозчик не найден")
            carrier_id = carrier.id
        try:
            return registry.get_adapter(carrier_code), carrier_id
        except LookupError:
            raise NotFound("Перевозчик не найден") from None

    async def _one(
        self,
        adapter: Any,
        *,
        carrier_code: str,
        carrier_id: UUID,
        external_id: str,
        events: list[Any],
        body: bytes,
        headers: dict[str, str],
    ) -> int:
        # Первая транзакция: только поиск. Окно закрывается вместе с ней,
        # и дальше оно уже не действует — записывать под ним нельзя.
        async with session_scope() as session:
            found = await ShipmentRepository(session).find_for_webhook(carrier_id, external_id)
            if found is None:
                # Не 404: перевозчик повторял бы доставку до посинения из-за
                # заказа, созданного вообще не через нас.
                log.info("webhook.unknown_shipment", carrier=carrier_code)
                return 0
            tenant_id = found.tenant_id
            shipment_id = found.id

        # Вторая транзакция: обычная, с тенантом. Те же права, что у запроса
        # пользователя этого тенанта, и ни правом больше.
        async with session_scope(tenant_id) as session:
            secret = await self._secret(session, carrier_id)
            if not adapter.verify_webhook(body, headers, secret):
                log.warning("webhook.bad_signature", carrier=carrier_code)
                raise AuthenticationError("Подпись вебхука не сошлась")

            shipment = await ShipmentRepository(session).get(shipment_id)
            if shipment is None:
                # Между транзакциями отправление удалить некому, но если это
                # случилось — молча, а не пятисоткой.
                return 0
            return await TrackingService(session, self._settings).ingest(
                shipment, events, carrier_code=carrier_code, source=EventSource.WEBHOOK
            )

    async def _secret(self, session: Any, carrier_id: UUID) -> str:
        """Секрет подписи из учётной записи тенанта у этого перевозчика."""
        accounts = await CarrierAccountRepository(session).list_active()
        for account in accounts:
            if account.carrier_id != carrier_id:
                continue
            secret = decrypt_credentials(account, self._settings).get(CREDENTIAL_FIELD)
            if secret:
                return secret
        # Без секрета проверить подпись нечем, а принимать непроверенное
        # событие в ленту нельзя: она — основание для Carrier Score и разбора.
        raise AuthenticationError("Подпись вебхука не сошлась")
