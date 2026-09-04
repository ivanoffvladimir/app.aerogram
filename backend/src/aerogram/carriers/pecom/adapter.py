"""Адаптер ПЭК.

Стадии 1–2: авторизация, расчёт, оформление, поиск, трекинг и печатные
формы. Отмена не реализована намеренно — см. ниже.

Написан по **официальной документации перевозчика** — планка ADR-0020.
Машинной спецификации у ПЭК нет: справка `kabinet.pecom.ru/api/v1` —
серверный HTML, и это проверено чтением файла, а не поиском. Поэтому каждое
поле сверено с текстом справки и с официальными примерами запросов, которые
перевозчик публикует там же; и то и другое лежит в
`docs/integrations/sources/pecom/`.

Три решения, следующие прямо из контракта.

**Отмена есть, но не та.** У ПЭК два метода: `/order/cancellation/`
аннулирует заявку — но «не ранее, чем через 5 – 10 минут после подачи»
и только «до момента её планирования в маршрутном листе водителя», причём
пакетно, массивом кодов груза; а `/cargos/cancelandreturncargo/` — вовсе
не отмена, а платный возврат отправителю. Синхронное «да/нет» нашего
`CancelResult` не описывает ни одно из двух, поэтому
`supports_cancel = False` (ADR-0020, решение 4).

**Вебхуков у ПЭК нет.** Ни в одном из 18 разделов справки не описан ни приём
события, ни подписка на него. Значит трекинг живёт на опросе, а
`supports_webhooks = False` — это факт документации, а не заглушка.

**Ошибка приходит с кодом 200.** Логическая ошибка возвращается успешным
кодом состояния и конвертом `{"error": {...}}` в теле — см. `client`.
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
from aerogram.carriers.pecom.client import PecomClient
from aerogram.carriers.pecom.orders import (
    LIST_ORDERS_PATH,
    PRINT_PATH,
    STATUS_HISTORY_PATH,
    SUBMIT_PATH,
    create_payload,
    list_orders_payload,
    parse_created,
    parse_found,
    parse_printable,
    parse_statuses,
    print_payload,
    status_history_payload,
)
from aerogram.carriers.pecom.quotes import (
    CALCULATE_PATH,
    build_quote_payload,
    parse_quotes,
    tariff_types_for,
)
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierError, CarrierNotConfigured, CarrierValidationError
from aerogram.shared.logging import get_logger

__all__ = ["PECOM_CODE", "PecomAdapter"]

log = get_logger(__name__)

PECOM_CODE = "pecom"


class PecomAdapter:
    """Реализация ``CarrierAdapter`` для ПЭК."""

    code = PECOM_CODE
    name = "ПЭК"
    capabilities = Capabilities(
        # Вебхуков в документации ПЭК нет ни в одном разделе: трекинг
        # работает опросом.
        supports_webhooks=False,
        supports_pickup_request=True,
        supports_cod=False,
        supports_insurance=True,
        # Отмена есть, но не та — см. строку документации модуля.
        supports_cancel=False,
        supports_terminals=True,
        # Платный вес считает перевозчик: ему передаются габариты, вес
        # и объём каждого места, делителя в контракте нет (FR-1.2).
        computes_volumetric_weight=True,
        max_places=255,
        supported_label_formats=(LabelFormat.PDF_A4,),
    )

    def __init__(self, client_factory: Any = None) -> None:
        """``client_factory`` подменяется в тестах; в проде клиент строится сам."""
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client(acc: CarrierAccount) -> PecomClient:
        credentials = acc.credentials
        missing = [k for k in ("login", "api_key") if not credentials.get(k)]
        if missing:
            raise CarrierValidationError(
                f"В учётной записи ПЭК не заданы: {', '.join(missing)}",
                carrier_code=PECOM_CODE,
            )
        return PecomClient(
            login=credentials["login"],
            api_key=credentials["api_key"],
            is_sandbox=acc.is_sandbox,
        )

    # --- Расчёт -----------------------------------------------------------

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        """Расчёт по запрошенным продуктам ПЭК.

        Один вызов на все тарифы: метод принимает массив ``types`` и отдаёт
        массив ``transfers``. Тариф, который перевозчик считать отказался,
        в выдачу не попадает — у него нет цены.
        """
        payload = build_quote_payload(req, tariff_types_for(req))
        client = self._client_factory(acc)
        try:
            body = await client.post(CALCULATE_PATH, payload, operation="quote")
        finally:
            await client.aclose()

        try:
            return parse_quotes(body, price_source=acc.price_source)
        except ValueError as exc:
            # Неизвестная валюта — не повод отдать сумму без валюты.
            raise CarrierError(str(exc), carrier_code=PECOM_CODE) from exc

    # --- Оформление -------------------------------------------------------

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        """Оформить предварительную заявку.

        В отличие от Деловых Линий результат **не** ``is_pending``: ПЭК
        возвращает код груза сразу, и именно он служит идентификатором
        во всех остальных методах.
        """
        description = req.extras.get("cargo_description")
        if not isinstance(description, str) or not description.strip():
            # Поле объявлено обязательным, а его значения перевозчик ведёт
            # в собственном справочнике «Характер груза». Подставить своё
            # значит оформить не тот груз.
            raise CarrierValidationError(
                "Для оформления у ПЭК нужно наименование груза (cargo_description) "
                "из справочника перевозчика «Характер груза»",
                field="cargo_description",
                carrier_code=PECOM_CODE,
            )

        client = self._client_factory(acc)
        try:
            body = await client.post(
                SUBMIT_PATH,
                create_payload(req, description=description.strip()),
                operation="create",
            )
        finally:
            await client.aclose()
        try:
            return parse_created(body, number=req.number)
        except ValueError as exc:
            raise CarrierError(str(exc), carrier_code=PECOM_CODE) from exc

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        """Найти заказ по нашему номеру для сверки «призраков».

        Метода «найти по номеру клиента» у ПЭК нет, поэтому берётся журнал
        за узкое окно по дате подачи заявки и в нём ищется наш
        ``orderNumber`` (FR-2.5).
        """
        client = self._client_factory(acc)
        try:
            body = await client.post(LIST_ORDERS_PATH, list_orders_payload(), operation="find")
        finally:
            await client.aclose()
        return parse_found(body, number=number)

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        """История статусов груза."""
        client = self._client_factory(acc)
        try:
            body = await client.post(
                STATUS_HISTORY_PATH, status_history_payload((ext_id,)), operation="track"
            )
        finally:
            await client.aclose()
        return parse_statuses(body)

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        """Этикетка груза в PDF."""
        if fmt is not LabelFormat.PDF_A4:
            raise CarrierValidationError(
                f"ПЭК отдаёт печатные формы только в PDF, запрошен {fmt.value}",
                field="format",
                carrier_code=PECOM_CODE,
            )
        client = self._client_factory(acc)
        try:
            body = await client.post_raw(PRINT_PATH, print_payload(ext_id), operation="label")
        finally:
            await client.aclose()

        content = parse_printable(body)
        if content is None:
            # Форма ещё не готова либо ответ не разобран. Пустой PDF выдавать
            # нельзя, а падать не за что: контракт предусматривает ожидание.
            return LabelResult(format=LabelFormat.PDF_A4, content=None, is_pending=True)
        return LabelResult(format=LabelFormat.PDF_A4, content=content, is_pending=False)

    # --- Ещё не реализовано ----------------------------------------------

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        raise CarrierNotConfigured(
            "У ПЭК две операции отмены, и обе не описываются синхронным «да/нет»: "
            "аннулирование заявки пакетом кодов груза с окном 5–10 минут и платный "
            "возврат отправителю. Расширение контракта — отдельное решение "
            "(ADR-0020, решение 4)",
            carrier_code=PECOM_CODE,
        )

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        raise CarrierNotConfigured(
            "Выгрузка справочников ПЭК ещё не реализована", carrier_code=PECOM_CODE
        )

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        """У ПЭК вебхуков нет — ни приёма события, ни подписки на него.

        Пустой список, а не исключение: приёмник вебхуков не должен падать
        от чужого запроса, пришедшего не по адресу.
        """
        log.warning("pecom.webhook_unexpected", keys=sorted(payload)[:10])
        return []

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        raise CarrierNotConfigured(
            "ПЭК вебхуков не отправляет: в его документации нет ни приёма события, "
            "ни подписки. Трекинг работает опросом",
            carrier_code=PECOM_CODE,
        )
