"""Адаптер СДЭК.

Неделя 5 плана: расчёт стоимости и срока (``quote``) и нормализация тарифов.
Создание, отмена, печатные формы и трекинг — недели 6–7; их методы объявлены
и честно сообщают, что ещё не реализованы, вместо того чтобы молча возвращать
пустой результат.

К базе данных адаптер не обращается (ADR-0005) и справочники не записывает,
а отдаёт (ADR-0009). Коды городов СДЭК приходят в DTO уже разрешёнными:
их подставляет ``directories`` по ``city_carrier_map``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from aerogram.carriers.base import (
    CancelResult,
    Capabilities,
    CarrierAccount,
    CarrierCity,
    LabelResult,
    Party,
    Quote,
    QuoteRequest,
    RawEvent,
    RefCatalog,
    ShipmentRequest,
    ShipmentResult,
    WebhookUpdate,
)
from aerogram.carriers.cdek.client import CdekClient
from aerogram.carriers.cdek.mapping import (
    CDEK_CURRENCY_RUB,
    CDEK_TYPE_DELIVERY,
    DELIVERY_MODES,
    grams_from_kg,
    modes_for_request,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money

__all__ = ["CDEK_CODE", "CdekAdapter"]

log = get_logger(__name__)

CDEK_CODE = "cdek"

_TARIFF_LIST_PATH = "/calculator/tarifflist"
_CITIES_PATH = "/location/cities"


class CdekAdapter:
    """Реализация ``CarrierAdapter`` для СДЭК."""

    code = CDEK_CODE
    name = "СДЭК"
    capabilities = Capabilities(
        supports_webhooks=True,
        supports_pickup_request=True,
        supports_cod=True,
        supports_insurance=True,
        supports_cancel=True,
        supports_terminals=True,
        # СДЭК считает объёмный вес сам, по габаритам мест. Досчитывать его
        # на нашей стороне значило бы учесть его дважды (FR-1.2).
        computes_volumetric_weight=True,
        max_places=255,
        supported_label_formats=(LabelFormat.PDF_A4, LabelFormat.PDF_A5, LabelFormat.PDF_A6),
    )

    def __init__(self, client_factory: Any = None) -> None:
        """``client_factory`` подменяется в тестах; в проде клиент строится сам."""
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client(acc: CarrierAccount) -> CdekClient:
        credentials = acc.credentials
        missing = [k for k in ("client_id", "client_secret") if not credentials.get(k)]
        if missing:
            raise CarrierValidationError(
                f"В учётной записи СДЭК не заданы: {', '.join(missing)}", carrier_code=CDEK_CODE
            )
        return CdekClient(
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
            is_sandbox=acc.is_sandbox,
        )

    # --- Расчёт -----------------------------------------------------------

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        """Расчёт по доступным тарифам.

        Возвращает по одной котировке на тариф. Тарифы, чей режим доставки
        не отвечает запросу, отбрасываются: показать цену до пункта выдачи
        тому, кто просил доставку до двери, значит дать заведомо неверную цену,
        которая к тому же всегда ниже и потому выигрывает ранжирование.
        """
        payload = self._quote_payload(req)
        client = self._client_factory(acc)
        try:
            body = await client.post(_TARIFF_LIST_PATH, payload, operation="quote")
        finally:
            await client.aclose()

        self._raise_for_errors(body)

        wanted = modes_for_request(pickup=req.pickup, delivery_to_door=req.delivery_to_door)
        quotes: list[Quote] = []
        skipped_modes: list[int] = []

        for row in body.get("tariff_codes") or []:
            mode_raw = row.get("delivery_mode")
            if mode_raw not in wanted:
                skipped_modes.append(mode_raw)
                continue
            quote = self._to_quote(row, acc)
            if quote is not None:
                quotes.append(quote)

        if skipped_modes:
            # Не молча: если СДЭК добавит новый режим, это должно быть видно
            # в логах, а не выглядеть как «тарифов стало меньше».
            log.info(
                "cdek.quote_modes_filtered",
                kept=len(quotes),
                skipped=len(skipped_modes),
                modes=sorted({m for m in skipped_modes if m is not None}),
            )
        return quotes

    def _quote_payload(self, req: QuoteRequest) -> dict[str, Any]:
        return {
            "type": CDEK_TYPE_DELIVERY,
            "currency": CDEK_CURRENCY_RUB,
            "lang": "rus",
            "date": utcnow().isoformat(timespec="seconds"),
            "from_location": self._location(req.sender),
            "to_location": self._location(req.recipient),
            "packages": [
                {
                    "weight": grams_from_kg(place.weight_kg),
                    "length": place.length_cm,
                    "width": place.width_cm,
                    "height": place.height_cm,
                }
                for place in req.places
            ],
        }

    @staticmethod
    def _location(party: Party) -> dict[str, Any]:
        """Пункт в терминах СДЭК.

        Порядок предпочтений от точного к приблизительному: собственный код
        города СДЭК, затем индекс, затем название с кодом страны. Код точен
        и не зависит от написания; название — последнее средство, потому что
        одноимённых населённых пунктов в России десятки.
        """
        if party.carrier_city_code:
            return {"code": int(party.carrier_city_code)}
        if party.postal_code:
            return {"postal_code": party.postal_code}
        return {"country_code": "RU", "city": party.city_name}

    def _to_quote(self, row: dict[str, Any], acc: CarrierAccount) -> Quote | None:
        """Строка ответа СДЭК → котировка.

        Строка без цены или без кода тарифа отбрасывается: подставить ноль
        значило бы вывести её в выдачу первой строкой как самую дешёвую.
        """
        tariff_code = row.get("tariff_code")
        delivery_sum = row.get("delivery_sum")
        if tariff_code is None or delivery_sum is None:
            log.warning("cdek.quote_row_incomplete", tariff_code=tariff_code)
            return None

        period_min = int(row.get("period_min") or 0)
        period_max = int(row.get("period_max") or period_min)
        mode = DELIVERY_MODES.get(int(row.get("delivery_mode") or 0))

        return Quote(
            service_code=str(tariff_code),
            tariff_code=str(tariff_code),
            service_name=str(row.get("tariff_name") or f"Тариф {tariff_code}"),
            # Деньги только через строку: float из JSON уже потерял точность,
            # и Decimal(float) закрепил бы потерю. Валюта не угадывается —
            # расчёт запрошен в рублях (CDEK_CURRENCY_RUB в теле запроса),
            # значит и ответ в рублях.
            price=Money.from_major(str(delivery_sum), "RUB"),
            transit_days_min=period_min,
            transit_days_max=period_max,
            promised_delivery_date=self._promised_date(row),
            price_source=acc.price_source,
            raw={
                "tariff_description": row.get("tariff_description"),
                "delivery_mode": row.get("delivery_mode"),
                "normalized_mode": mode.value if mode else None,
                "calendar_min": row.get("calendar_min"),
                "calendar_max": row.get("calendar_max"),
            },
        )

    @staticmethod
    def _promised_date(row: dict[str, Any]) -> date | None:
        """Плановая дата доставки из календарных дней СДЭК.

        Берутся именно ``calendar_max``, а не рабочие ``period_max``: плановая
        дата — это обещание клиенту в календаре, и сравнивать с фактом её нужно
        по календарю. Пересчёт в рабочие дни делает домен при разборе факта
        (FR-6.2).
        """
        calendar_max = row.get("calendar_max")
        if calendar_max is None:
            return None
        return utcnow().date() + timedelta(days=int(calendar_max))

    @staticmethod
    def _raise_for_errors(body: dict[str, Any]) -> None:
        """Ошибки уровня запроса СДЭК → доменное исключение.

        Вызывающий слой превращает его в отдельную строку выдачи с русским
        текстом: ошибка одного перевозчика не роняет расчёт (FR-1.4).
        """
        errors = body.get("errors") or []
        if not errors:
            return
        first = errors[0] if isinstance(errors, list) else {}
        code = str(first.get("code") or "unknown")
        message = str(first.get("message") or "Перевозчик отклонил запрос расчёта")
        log.info("cdek.quote_rejected", cdek_code=code)
        raise CarrierValidationError(message, carrier_code=CDEK_CODE)

    # --- Справочники ------------------------------------------------------

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        """Города СДЭК с их кодами.

        Терминалы и услуги появятся вместе с созданием отправлений (неделя 7):
        пустой кортеж означает «этот справочник пока не выгружается», и домен
        на нём ничего не гасит (ADR-0009).
        """
        client = self._client_factory(acc)
        try:
            body = await client.post(
                _CITIES_PATH, {"country_codes": ["RU"], "size": 1000}, operation="cities"
            )
        finally:
            await client.aclose()

        rows = body.get("_list") if isinstance(body.get("_list"), list) else body.get("cities")
        cities = tuple(
            CarrierCity(
                code=str(item["code"]),
                name=str(item.get("city") or ""),
                region=item.get("region"),
                fias_id=item.get("fias_guid"),
                kladr_id=item.get("kladr_code"),
            )
            for item in (rows or [])
            if item.get("code") is not None
        )
        return RefCatalog(cities=cities)

    # --- Ещё не реализовано (недели 6-7) ----------------------------------

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        raise self._not_implemented("создание отправления", "неделя 6")

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        raise self._not_implemented("отмена отправления", "неделя 6")

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        raise self._not_implemented("сверка «призраков»", "неделя 6")

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        raise self._not_implemented("печатная форма", "неделя 7")

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        raise self._not_implemented("трекинг", "неделя 8")

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        raise self._not_implemented("приём вебхуков", "неделя 8")

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        raise self._not_implemented("проверка подписи вебхука", "неделя 8")

    @staticmethod
    def _not_implemented(what: str, when: str) -> CarrierError:
        """Явный отказ вместо пустого результата.

        Пустой список или ``None`` здесь выглядели бы как «перевозчик ничего
        не вернул» и разошлись бы по домену как данные.
        """
        return CarrierError(f"СДЭК: {what} ещё не реализовано ({when})", carrier_code=CDEK_CODE)
