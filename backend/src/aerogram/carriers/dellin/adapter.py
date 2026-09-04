"""Адаптер Деловых Линий.

Стадия 1: авторизация и расчёт. Остальные методы объявлены и честно
сообщают, что ещё не реализованы, вместо того чтобы молча возвращать пустоту.

Написан по **официальной OpenAPI 3.0.3 перевозчика** — планка ADR-0020.
Спека лежит в репозитории (`docs/integrations/sources/dellin/schema.yaml`),
и расхождение кода с ней есть ошибка кода.

Три решения, следующие прямо из контракта.

**Возможности.** ``supports_cancel=False``: отмены заказа целиком
у Деловых Линий не существует. В теге «Управление заказом» есть только
``cancel_delivery`` и ``cancel_pickup``, оба асинхронные — «изменения
вступают в силу не сразу, после проверки они могут быть одобрены или
отклонены», — а у отмены доставки ещё и окно «до 17:00 по местному времени
дня, предшествующего дню доставки». Синхронное «да/нет» нашего
``CancelResult`` не описывает ни одну из этих операций (ADR-0020, решение 4).

``supports_webhooks=True``: у перевозчика восемь методов ``/v1/webhooks/*``
и справочник типов событий. Разбор вебхука — следующая стадия; чем
подтверждается подлинность события, спека не говорит, и до выяснения
действует ADR-0015.

**Договор обязывает к сессии.** Расчёт принимает ``sessionID`` необязательным:
без него возвращаются публичные тарифы, с ним — персональные скидки
контрагента. Значит для ``mode="own_contract"`` сессия обязательна: иначе
платформа выдала бы публичный тариф, подписав его как цену по договору
клиента. Это ложь в денежном пути, и она запрещена явной проверкой.

**Договорная цена не становится предложением.** См. ``quotes``.
"""

from __future__ import annotations

from typing import Any

from aerogram.carriers.base import (
    CancelResult,
    Capabilities,
    CarrierAccount,
    LabelResult,
    Quote,
    QuoteRequest,
    RawEvent,
    RefCatalog,
    ShipmentRequest,
    ShipmentResult,
    WebhookUpdate,
)
from aerogram.carriers.dellin.client import DellinClient, dellin_error
from aerogram.carriers.dellin.quotes import (
    CALCULATOR_PATH,
    ContractPriceError,
    build_quote_payload,
    delivery_types_for,
    parse_quote,
)
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierError, CarrierNotConfigured, CarrierValidationError
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money  # noqa: F401  (нужен в аннотациях будущих стадий)

__all__ = ["DELLIN_CODE", "DellinAdapter"]

log = get_logger(__name__)

DELLIN_CODE = "dellin"


class DellinAdapter:
    """Реализация ``CarrierAdapter`` для Деловых Линий."""

    code = DELLIN_CODE
    name = "Деловые Линии"
    capabilities = Capabilities(
        supports_webhooks=True,
        supports_pickup_request=True,
        supports_cod=False,
        supports_insurance=True,
        # Отмены заказа целиком не существует — см. строку документации модуля.
        supports_cancel=False,
        supports_terminals=True,
        # Платный вес считает перевозчик: ему передаются вес и объём
        # отдельными полями, делителя в контракте нет (FR-1.2).
        computes_volumetric_weight=True,
        max_places=255,
        supported_label_formats=(LabelFormat.PDF_A4,),
    )

    def __init__(self, client_factory: Any = None) -> None:
        """``client_factory`` подменяется в тестах; в проде клиент строится сам."""
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client(acc: CarrierAccount) -> DellinClient:
        credentials = acc.credentials
        appkey = credentials.get("appkey")
        if not appkey:
            raise CarrierValidationError(
                "В учётной записи Деловых Линий не задан ключ приложения (appkey)",
                carrier_code=DELLIN_CODE,
            )
        return DellinClient(
            appkey=appkey,
            pat=credentials.get("pat") or None,
            login=credentials.get("login") or None,
            password=credentials.get("password") or None,
        )

    # --- Расчёт -----------------------------------------------------------

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        """Расчёт по запрошенным видам перевозки.

        Один вызов калькулятора на вид перевозки: у Деловых Линий вид задаётся
        в запросе, а поле ``availableDeliveryTypes`` в ответе не документировано
        и в примере перевозчика не сходится с ценами — строить по нему выдачу
        значило бы угадывать цену.
        """
        client = self._client_factory(acc)
        try:
            self._require_session_for_contract(client, acc)
            quotes: list[Quote] = []
            for delivery_type in delivery_types_for(req):
                quote = await self._quote_one(client, req, acc, delivery_type)
                if quote is not None:
                    quotes.append(quote)
            return quotes
        finally:
            await client.aclose()

    def _require_session_for_contract(self, client: DellinClient, acc: CarrierAccount) -> None:
        """Цена по договору клиента требует входа в его кабинет.

        Без сессии перевозчик вернёт публичный тариф. Выдать его как цену
        по договору — это неверная цена в снимке решения, на который потом
        опирается сверка счетов.
        """
        if acc.mode == "own_contract" and not client.can_authorize:
            raise CarrierValidationError(
                "Для расчёта по договору с Деловыми Линиями нужен токен личного кабинета "
                "или пара логин-пароль: без входа перевозчик отдаёт публичный тариф",
                carrier_code=DELLIN_CODE,
            )

    async def _quote_one(
        self,
        client: DellinClient,
        req: QuoteRequest,
        acc: CarrierAccount,
        delivery_type: str,
    ) -> Quote | None:
        payload = build_quote_payload(req, delivery_type)
        body = await client.post(
            CALCULATOR_PATH,
            payload,
            operation="quote",
            with_session=client.can_authorize,
        )

        message = dellin_error(body)
        if message:
            raise CarrierError(message, carrier_code=DELLIN_CODE)

        try:
            return parse_quote(
                body,
                delivery_type=delivery_type,
                to_door=req.delivery_to_door,
                price_source=acc.price_source,
            )
        except ContractPriceError as exc:
            # Договорная цена — законный ответ, но не котировка. Она видна
            # в логах и не притворяется сбоем перевозчика.
            log.info("dellin.contract_price", delivery_type=delivery_type)
            raise CarrierValidationError(str(exc), carrier_code=DELLIN_CODE) from exc
        except ValueError as exc:
            log.warning("dellin.quote_unparsable", delivery_type=delivery_type, reason=str(exc))
            return None

    # --- Ещё не реализовано ----------------------------------------------

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        raise CarrierNotConfigured(
            "Оформление заказа у Деловых Линий ещё не реализовано", carrier_code=DELLIN_CODE
        )

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        raise CarrierNotConfigured(
            "Печатные формы Деловых Линий ещё не реализованы", carrier_code=DELLIN_CODE
        )

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        raise CarrierNotConfigured(
            "Трекинг Деловых Линий ещё не реализован", carrier_code=DELLIN_CODE
        )

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        raise CarrierNotConfigured(
            "У Деловых Линий нет отмены заказа целиком: есть отмена забора и отмена "
            "доставки, обе асинхронные. Расширение контракта — отдельное решение "
            "(ADR-0020, решение 4)",
            carrier_code=DELLIN_CODE,
        )

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        raise CarrierNotConfigured(
            "Поиск заказа Деловых Линий по номеру ещё не реализован", carrier_code=DELLIN_CODE
        )

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        raise CarrierNotConfigured(
            "Выгрузка справочников Деловых Линий ещё не реализована", carrier_code=DELLIN_CODE
        )

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        """Разбор вебхука появится вместе с трекингом.

        Пустой список, а не исключение: приёмник вебхуков не должен падать
        на перевозчике, чей разбор ещё не написан.
        """
        log.warning("dellin.webhook_ignored", keys=sorted(payload)[:10])
        return []

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        raise CarrierNotConfigured(
            "Деловые Линии: чем подтверждается подлинность вебхука, в спецификации "
            "не сказано. Пока это не выяснено, непроверенное событие в ленту "
            "не попадает (ADR-0015)",
            carrier_code=DELLIN_CODE,
        )
