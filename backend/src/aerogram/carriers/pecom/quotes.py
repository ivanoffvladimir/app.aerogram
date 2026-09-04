"""Расчёт ПЭК: сборка ``calculator/calculateprice`` и разбор ответа.

Здесь живут решения про деньги, поэтому они собраны в одном месте
(CLAUDE.md §7, пункт 5).

**Один вызов — несколько предложений.** В отличие от Деловых Линий, ПЭК
принимает массив тарифов (`types`) и возвращает массив `transfers`, по одному
на тариф. Значит рейт-шоппинг по ПЭК стоит одного запроса, а не одного
на тариф.

**Тариф с ошибкой — не предложение.** Каждый элемент `transfers` несёт свои
`hasError` и `errorMessage`, и в примере самого перевозчика один из тарифов
приходит с текстом «Длина груза превышает допустимую для АВИА». Такой элемент
пропускается: цены у него нет, а ноль вывел бы его первой строкой.

**Валюта берётся из ответа, а не подразумевается.** ПЭК возвращает числовой
код ISO (`"643"`). Неизвестный код означает отказ от предложения: сумма
в тенге, посчитанная как рублёвая, выигрывает любое сравнение.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from aerogram.carriers.base import Party, Quote, QuoteRequest
from aerogram.carriers.pecom.mapping import (
    DEFAULT_TARIFF_TYPES,
    PECOM_CURRENCY_RUB,
    TARIFF_NAMES,
    cargos_block,
    currency_from_code,
    declared_value_rub,
    money_from_response,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import PriceSource
from aerogram.shared.logging import get_logger

__all__ = ["CALCULATE_PATH", "build_quote_payload", "parse_quotes", "tariff_types_for"]

log = get_logger(__name__)

CALCULATE_PATH: Final = "/calculator/calculateprice/"

#: Формат даты плановой передачи груза: местное время филиала отправления,
#: без смещения. Так он записан и в документации, и во всех официальных
#: примерах ПЭК (`"plannedDateTime": "2024-03-28T10:00:00"`).
_PLANNED_FORMAT: Final = "%Y-%m-%dT%H:%M:%S"


def tariff_types_for(req: QuoteRequest) -> tuple[int, ...]:
    """Какие продукты ПЭК считать.

    По умолчанию — LTL Авто. Вызывающий может расширить набор через
    ``extras["tariff_types"]``; неизвестные значения отбрасываются, а тариф 5
    («ПЭК:Express Авто») не принимается никогда: документация прямо
    предупреждает, что метод расчёта его не поддерживает.
    """
    raw = req.extras.get("tariff_types")
    if not isinstance(raw, list | tuple) or not raw:
        return DEFAULT_TARIFF_TYPES
    wanted: list[int] = []
    for item in raw:
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if code in TARIFF_NAMES and code not in wanted:
            wanted.append(code)
    if not wanted:
        log.warning("pecom.unknown_tariff_types", requested=list(raw))
        return DEFAULT_TARIFF_TYPES
    return tuple(wanted)


def _warehouse_id(party: Party) -> str | None:
    """Идентификатор склада ПЭК.

    Это GUID филиала, а не название города. Сопоставление ведёт
    ``directories`` через ``city_carrier_map`` (ADR-0009), поэтому адаптер
    берёт готовое значение и ничего не угадывает.
    """
    return party.terminal_code or party.carrier_city_code


def build_quote_payload(req: QuoteRequest, tariff_types: tuple[int, ...]) -> dict[str, Any]:
    """Тело ``POST /calculator/calculateprice/``.

    Забор и доставка включаются ровно тогда, когда их просили: у ПЭК это
    отдельные услуги со своей стоимостью, и посчитать их «на всякий случай»
    значит завысить цену и проиграть сравнение по чужой вине.
    """
    payload: dict[str, Any] = {
        "currencyCode": PECOM_CURRENCY_RUB,
        "types": list(tariff_types),
        "cargos": cargos_block(req.places),
        # Плановая передача груза: сейчас, если срок не задан. Дата влияет
        # на расчёт, поэтому передаётся всегда, а не опускается.
        "plannedDateTime": utcnow().strftime(_PLANNED_FORMAT),
        "isPickUp": req.pickup,
        "isDelivery": req.delivery_to_door,
        "isInsurance": req.insurance,
    }
    if req.insurance:
        payload["isInsurancePrice"] = declared_value_rub(req.declared_value)

    sender_warehouse = _warehouse_id(req.sender)
    receiver_warehouse = _warehouse_id(req.recipient)
    if sender_warehouse:
        payload["senderWarehouseId"] = sender_warehouse
    if receiver_warehouse:
        payload["receiverWarehouseId"] = receiver_warehouse

    if req.pickup:
        payload["pickup"] = {"address": req.sender.address or req.sender.city_name}
    if req.delivery_to_door:
        payload["delivery"] = {"address": req.recipient.address or req.recipient.city_name}
    return payload


def _breakdown(services: object, currency: str) -> dict[str, Any]:
    """Расшифровка стоимости из массива услуг.

    ПЭК возвращает услуги деревом: у элемента может быть вложенный массив
    ``services``, чья стоимость **не входит** в стоимость родителя — так
    сказано в документации. Поэтому дерево разворачивается целиком.
    """
    result: dict[str, Any] = {}
    stack = list(services) if isinstance(services, list) else []
    while stack:
        item = stack.pop(0)
        if not isinstance(item, dict):
            continue
        nested = item.get("services")
        if isinstance(nested, list):
            stack.extend(nested)
        amount = money_from_response(item.get("cost"), currency)
        label = str(item.get("info") or item.get("serviceType") or "").strip().rstrip(":")
        if amount is None or not label:
            continue
        # Одноимённые услуги суммируются, а не затирают друг друга.
        result[label] = result[label] + amount if label in result else amount
    return result


def parse_quotes(body: dict[str, Any], *, price_source: PriceSource) -> list[Quote]:
    """Ответ расчёта → список котировок, по одной на успешный тариф."""
    currency = currency_from_code(body.get("currencyCode"))
    if currency is None:
        raise ValueError(f"ПЭК вернул неизвестный код валюты: {body.get('currencyCode')!r}")

    transfers = body.get("transfers")
    if not isinstance(transfers, list):
        return []

    today = utcnow().date()
    quotes: list[Quote] = []
    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue
        if transfer.get("hasError") is True:
            # Не молча: тариф, который перевозчик считать отказался, должен
            # быть виден в логах, а не выглядеть как «тарифов стало меньше».
            log.info(
                "pecom.tariff_refused",
                tariff=transfer.get("type"),
                reason=str(transfer.get("errorMessage") or "")[:200],
            )
            continue

        price = money_from_response(transfer.get("costTotal"), currency)
        tariff = transfer.get("type")
        if price is None or tariff is None:
            log.warning("pecom.transfer_incomplete", tariff=tariff)
            continue

        days_raw = transfer.get("estDeliveryTime")
        days = int(days_raw) if isinstance(days_raw, int | float) else 0
        code = str(int(tariff))
        quotes.append(
            Quote(
                service_code=code,
                tariff_code=code,
                service_name=TARIFF_NAMES.get(int(tariff), f"Тариф ПЭК {code}"),
                price=price,
                transit_days_min=days,
                transit_days_max=days,
                promised_delivery_date=today + timedelta(days=days) if days else None,
                price_source=price_source,
                raw=transfer,
                price_breakdown=_breakdown(transfer.get("services"), currency),
            )
        )
    return quotes
