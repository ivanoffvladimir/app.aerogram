"""Адаптер Деловых Линий.

Стадии 1–2: авторизация, расчёт, оформление, поиск заказа, трекинг
и печатные формы. Разбор входящего вебхука не реализован намеренно —
см. ниже, это не пропуск, а отсутствие источника.

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
from aerogram.carriers.dellin.orders import (
    ORDERS_PATH,
    PRINTABLE_PATH,
    REQUEST_PATH,
    STATUSES_HISTORY_PATH,
    create_payload,
    orders_payload,
    parse_created,
    parse_order,
    parse_printable,
    parse_statuses,
    printable_payload,
    statuses_payload,
    waybill_uid,
)
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

    # --- Оформление -------------------------------------------------------

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        """Оформить заявку на перевозку.

        Возвращает ``is_pending``: перевозчик отдаёт номер заявки, а номер
        заказа появляется после её обработки. Наш собственный номер уходит
        в ``cargoCode`` и ``orderNumber``, чтобы заказ потом можно было найти
        сверкой «призраков» (FR-2.5).
        """
        freight_uid = req.extras.get("freight_uid")
        if not isinstance(freight_uid, str) or not freight_uid:
            # Умолчание здесь было бы хуже отказа: перевозчик посчитает
            # и повезёт не тот характер груза.
            raise CarrierValidationError(
                "Для оформления у Деловых Линий нужен характер груза (freight_uid) "
                "из справочника перевозчика «Характер груза»",
                field="freight_uid",
                carrier_code=DELLIN_CODE,
            )

        client = self._client_factory(acc)
        try:
            body = await self._call(
                client, REQUEST_PATH, create_payload(req, freight_uid=freight_uid), "create"
            )
            try:
                return parse_created(body, number=req.number)
            except ValueError as exc:
                raise CarrierError(str(exc), carrier_code=DELLIN_CODE) from exc
        finally:
            await client.aclose()

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        """Найти заказ по нашему внутреннему номеру.

        Журнал заказов принимает ``orderNumber`` — «внутренний номер заказа
        клиента», — и это делает сверку «призраков» возможной без хранения
        чужих идентификаторов.
        """
        client = self._client_factory(acc)
        try:
            body = await self._call(client, ORDERS_PATH, orders_payload(number=number), "find")
            return parse_order(body)
        finally:
            await client.aclose()

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        """История статусов заказа."""
        client = self._client_factory(acc)
        try:
            body = await self._call(
                client, STATUSES_HISTORY_PATH, statuses_payload((ext_id,)), "track"
            )
            return parse_statuses(body)
        finally:
            await client.aclose()

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        """Накладная в PDF.

        Два вызова, а не один: печатная форма запрашивается по UID накладной,
        а он лежит в журнале заказов. Пока заявка не обработана, накладной
        не существует — тогда возвращается ``is_pending`` без содержимого,
        как того требует контракт (FR-4.5).
        """
        if fmt is not LabelFormat.PDF_A4:
            raise CarrierValidationError(
                f"Деловые Линии отдают накладную только в PDF, запрошен {fmt.value}",
                field="format",
                carrier_code=DELLIN_CODE,
            )

        client = self._client_factory(acc)
        try:
            orders = await self._call(
                client, ORDERS_PATH, orders_payload(doc_ids=(ext_id,)), "label"
            )
            doc_uid = waybill_uid(orders)
            if doc_uid is None:
                log.info("dellin.waybill_not_ready", external_id=ext_id)
                return LabelResult(format=LabelFormat.PDF_A4, content=None, is_pending=True)

            body = await self._call(client, PRINTABLE_PATH, printable_payload(doc_uid), "label")
            content = parse_printable(body)
            if content is None:
                return LabelResult(
                    format=LabelFormat.PDF_A4,
                    content=None,
                    is_pending=True,
                    external_ref=doc_uid,
                )
            return LabelResult(
                format=LabelFormat.PDF_A4, content=content, is_pending=False, external_ref=doc_uid
            )
        finally:
            await client.aclose()

    async def _call(
        self, client: DellinClient, path: str, payload: dict[str, Any], operation: str
    ) -> dict[str, Any]:
        """Вызов с проверкой конверта ошибок.

        Ошибка перевозчика никогда не даёт 500: она становится
        ``CarrierError`` с текстом, который можно показать оператору.
        """
        body = await client.post(
            path, payload, operation=operation, with_session=client.can_authorize
        )
        message = dellin_error(body)
        if message:
            raise CarrierError(message, carrier_code=DELLIN_CODE)
        return body

    # --- Ещё не реализовано ----------------------------------------------

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        raise CarrierNotConfigured(
            "У Деловых Линий нет отмены заказа целиком: есть отмена забора и отмена "
            "доставки, обе асинхронные. Расширение контракта — отдельное решение "
            "(ADR-0020, решение 4)",
            carrier_code=DELLIN_CODE,
        )

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        raise CarrierNotConfigured(
            "Выгрузка справочников Деловых Линий ещё не реализована", carrier_code=DELLIN_CODE
        )

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        """Разбор входящего вебхука невозможен: источника нет.

        Это не пропуск. В официальной спеке описаны восемь методов
        ``/v1/webhooks/*`` — но все они про **управление подпиской**:
        создать, изменить, удалить, посмотреть список событий. Формы тела,
        которое перевозчик присылает нам, в спецификации нет ни одной схемой.
        Планка ADR-0020 запрещает додумывать её по догадке.

        Пустой список, а не исключение: приёмник вебхуков не должен падать
        из-за перевозчика, чей формат события неизвестен. Событие при этом
        не теряется — трекинг работает опросом ``track``.
        """
        log.warning("dellin.webhook_shape_unknown", keys=sorted(payload)[:10])
        return []

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        raise CarrierNotConfigured(
            "Деловые Линии: чем подтверждается подлинность вебхука, в спецификации "
            "не сказано. Пока это не выяснено, непроверенное событие в ленту "
            "не попадает (ADR-0015)",
            carrier_code=DELLIN_CODE,
        )
