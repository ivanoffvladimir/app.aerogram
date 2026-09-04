"""Адаптер Почты России.

Стадия 1: авторизация и расчёт. Оформление, трекинг и печатные формы —
стадия 2; каждый метод объявлен и честно отказывает с причиной, а не молчит.

Написан по **официальной документации перевозчика** — планка ADR-0020.
Машинная спецификация у Почты есть только на трекинг (два WSDL, которые она
раздаёт сама), а расчёт и оформление описаны прозой: 117 страниц справки
API Онлайн-сервиса «Отправка», по одной на метод. Все они выкачаны
в `docs/integrations/sources/pochta/`, и каждое поле сверено с текстом.

Четыре решения, следующие прямо из источника.

**Боевого адреса API в документации нет.** Все страницы дают только
«Локальный URL» вида `/1.0/tariff`; полный адрес встречается ровно в одном
файле примеров. Поэтому боевой адрес задаётся в учётной записи тенанта,
а его отсутствие — отказ, а не подстановка выдуманного хоста (см. `client`).

**Один запрос — одна цена.** Выдачи списком у Почты нет: расчёт считает одно
сочетание вида РПО, категории и вида транспортировки. Рейт-шоппинг — это N
запросов, и набор продуктов объявлен явно, потому что каждый лишний тратит
суточную квоту (см. `mapping`).

**Отмены нет вовсе.** Ни в одном из 117 методов нет отмены отправления:
у «Отправки» есть удаление заказа из черновиков до формирования партии,
что нашим синхронным `CancelResult` не описывается. Это и есть
`supports_cancel = False` из решения 4 ADR-0020, подтверждённое чтением.

**Вебхуков нет.** Ни приёма события, ни подписки на него в справке нет:
трекинг Почты живёт на опросе, и делать его нечем до стадии 2 — SOAP-контракт
требует `zeep` вне `carriers/major`, то есть поправки к ADR-0013 и новой
зависимости, а это решение человека (CLAUDE.md §2).
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
from aerogram.carriers.pochta.client import BASE_URL_SETTING, PochtaClient, user_key
from aerogram.carriers.pochta.mapping import PochtaProduct
from aerogram.carriers.pochta.quotes import TARIFF_PATH, build_tariff_payload, parse_tariff
from aerogram.carriers.pochta.quotes import products_for as _products_for
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierNotConfigured, CarrierValidationError
from aerogram.shared.logging import get_logger

__all__ = ["POCHTA_CODE", "PochtaAdapter"]

log = get_logger(__name__)

POCHTA_CODE = "pochta"


class PochtaAdapter:
    """Реализация ``CarrierAdapter`` для Почты России."""

    code = POCHTA_CODE
    name = "Почта России"
    capabilities = Capabilities(
        # Ни приёма события, ни подписки в справке «Отправки» нет —
        # см. строку документации модуля.
        supports_webhooks=False,
        # Отметка «Курьер» в расчёте есть, но забор оформляется отдельным
        # методом стадии 2, поэтому здесь честное «нет».
        supports_pickup_request=False,
        # Наложенный платёж выражается категорией РПО, но поля для его суммы
        # в теле расчёта нет ни одного: обещать нечем.
        supports_cod=False,
        # Объявленная ценность — категория РПО плюс `declared-value`.
        supports_insurance=True,
        supports_cancel=False,
        supports_terminals=False,
        # Объёмного веса у Почты нет: тариф считается от массы, а за габариты
        # начисляется отдельная надбавка за негабарит. Досчитывать объёмный
        # вес на нашей стороне значило бы отправить перевозчику массу,
        # которой не существует, и получить цену чужого отправления (FR-1.2).
        computes_volumetric_weight=True,
        # Одно РПО — одно место: в теле расчёта одна `mass` и один `dimension`.
        max_places=1,
        # Печатные формы — стадия 2, обещать формат заранее нечем.
        supported_label_formats=(),
    )

    def __init__(self, client_factory: Any = None) -> None:
        """``client_factory`` подменяется в тестах; в проде клиент строится сам."""
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client(acc: CarrierAccount) -> PochtaClient:
        credentials = acc.credentials
        token = credentials.get("token") or ""
        if not token:
            raise CarrierValidationError(
                "В учётной записи Почты России не задан токен авторизации приложения",
                carrier_code=POCHTA_CODE,
            )
        base_url = acc.settings.get(BASE_URL_SETTING)
        return PochtaClient(
            token=token,
            user_auth_key=user_key(
                login=credentials.get("login"),
                password=credentials.get("password"),
                key=credentials.get("user_key"),
            ),
            base_url=base_url if isinstance(base_url, str) else None,
            is_sandbox=acc.is_sandbox,
        )

    # --- Расчёт -----------------------------------------------------------

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        """Расчёт по каждому запрошенному продукту.

        Продукт, который перевозчик считать отказался, гасит себя, а не всю
        выдачу по Почте: сочетаемость видов РПО с категориями и видами
        транспортировки нигде не документирована, и отказ по одному
        сочетанию — ожидаемый исход, а не сбой.
        """
        client = self._client_factory(acc)
        try:
            quotes: list[Quote] = []
            for product in _products_for(req):
                quote = await self._quote_one(client, req, acc, product)
                if quote is not None:
                    quotes.append(quote)
            return quotes
        finally:
            await client.aclose()

    async def _quote_one(
        self,
        client: PochtaClient,
        req: QuoteRequest,
        acc: CarrierAccount,
        product: PochtaProduct,
    ) -> Quote | None:
        payload = build_tariff_payload(req, product)
        body = await client.post(TARIFF_PATH, payload, operation="quote")
        return parse_tariff(body, product, price_source=acc.price_source)

    # --- Ещё не реализовано ----------------------------------------------

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        raise CarrierNotConfigured(
            "Оформление у Почты России ещё не реализовано: неизвестно, в какой момент "
            "появляется ШПИ — при создании заказа версии 2.0 или только после "
            "формирования партии, — а это определяет весь порядок оформления",
            carrier_code=POCHTA_CODE,
        )

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        raise CarrierNotConfigured(
            "Печатные формы Почты России ещё не реализованы", carrier_code=POCHTA_CODE
        )

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        raise CarrierNotConfigured(
            "Трекинг Почты России ещё не реализован: он живёт в отдельном SOAP-сервисе, "
            "и его подключение требует zeep вне carriers/major, то есть поправки "
            "к ADR-0013 и решения человека",
            carrier_code=POCHTA_CODE,
        )

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        raise CarrierNotConfigured(
            "У Почты России нет отмены отправления: до формирования партии заказ "
            "удаляется из черновиков, после — не отменяется вовсе. Синхронное "
            "«да/нет» нашего CancelResult этого не описывает (ADR-0020, решение 4)",
            carrier_code=POCHTA_CODE,
        )

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        raise CarrierNotConfigured(
            "Поиск заказа Почты России по нашему номеру ещё не реализован",
            carrier_code=POCHTA_CODE,
        )

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        raise CarrierNotConfigured(
            "Выгрузка справочника отделений Почты России ещё не реализована",
            carrier_code=POCHTA_CODE,
        )

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        """Вебхуков у Почты нет — разбирать нечего.

        Пустой список, а не исключение: приёмник вебхуков не должен падать
        из-за события, пришедшего непонятно от кого. Событие при этом
        не теряется — трекинг у Почты и так работает опросом.
        """
        log.warning("pochta.webhook_unexpected", keys=sorted(payload)[:10])
        return []

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        raise CarrierNotConfigured(
            "Почта России вебхуков не присылает: подтверждать подлинность нечего",
            carrier_code=POCHTA_CODE,
        )
