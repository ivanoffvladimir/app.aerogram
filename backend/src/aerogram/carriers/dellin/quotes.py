"""Расчёт Деловых Линий: сборка запроса ``/v2/calculator`` и разбор ответа.

Разбор вынесен из адаптера, потому что здесь живут решения про деньги,
и их надо читать в одном месте (CLAUDE.md §7, пункт 5).

Три из них стоит назвать прямо.

**Договорная цена — не предложение.** У Деловых Линий есть флаг
``contractPrice``, и в их собственном примере ответа он стоит вместе с
``"price": null`` и сообщением «После оформления заказа наш специалист
свяжется с Вами для утверждения стоимости». Такой ответ **не превращается
в котировку**: подставить ноль или пропустить цену значит вывести строку
первой как самую дешёвую, а Decision Engine сравнит несравнимое.

**Итоговая цена — ``data.price``**, а не сумма плеч и не число из
``availableDeliveryTypes``. У последнего в спеке нет ни описания, ни единиц,
а в примере перевозчика его значения не сходятся ни с ``price``, ни с ценами
по видам перевозки. Пока смысл поля не подтверждён живым ответом, оно
не используется: ошибка тут даёт цену, отличающуюся втрое.

**Срок — из ``orderDates``**, и поле зависит от того, куда везём: до двери
или до терминала. Имена полей у перевозчика написаны непоследовательно
(``derrivalToAddress`` с двумя «r», рядом ``derivalToAddressMax`` с одной),
поэтому они перечислены буквально, как в спеке.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

from aerogram.carriers.base import Party, Quote, QuoteRequest
from aerogram.carriers.dellin.mapping import (
    DEFAULT_DELIVERY_TYPE,
    DELIVERY_TYPES,
    cargo_block,
    money_from_response,
    parse_carrier_date,
)
from aerogram.shared.enums import PriceSource
from aerogram.shared.logging import get_logger

__all__ = [
    "CALCULATOR_PATH",
    "ContractPriceError",
    "build_quote_payload",
    "delivery_types_for",
    "parse_quote",
]

log = get_logger(__name__)

CALCULATOR_PATH: Final = "/v2/calculator.json"

#: Дата доставки до адреса получателя: минимальная и максимальная.
#: Написание полей — как у перевозчика, включая расхождение в числе «r».
_DOOR_DATE_FIELDS: Final = ("derrivalToAddress", "derivalToAddressMax")
#: Выдача на терминале получателя.
_TERMINAL_DATE_FIELDS: Final = ("giveoutFromOspReceiver", "giveoutFromOspReceiverMax")


class ContractPriceError(Exception):
    """Перевозчик согласен везти, но цену назовёт человек.

    Не ошибка перевозчика и не сбой связи: законный ответ, который просто
    не является котировкой. Поднимается, чтобы вызывающий положил его
    в ``failures`` с внятной причиной, а не потерял молча.
    """

    def __init__(self, delivery_type: str) -> None:
        super().__init__(
            f"Деловые Линии считают перевозку «{delivery_type}» по договорной цене: "
            "стоимость подтверждает менеджер, автоматический расчёт её не даёт"
        )
        self.delivery_type = delivery_type


def delivery_types_for(req: QuoteRequest) -> tuple[str, ...]:
    """Какие виды перевозки считать.

    По умолчанию — автодоставка. Вызывающий может расширить набор через
    ``extras["delivery_types"]``; неизвестные значения отбрасываются, потому
    что перевозчик ответит на них ошибкой на весь запрос.
    """
    raw = req.extras.get("delivery_types")
    if not isinstance(raw, list | tuple) or not raw:
        return (DEFAULT_DELIVERY_TYPE,)
    wanted = tuple(str(item) for item in raw if str(item) in DELIVERY_TYPES)
    if not wanted:
        log.warning("dellin.unknown_delivery_types", requested=list(raw))
        return (DEFAULT_DELIVERY_TYPE,)
    return wanted


def _point(party: Party, *, to_address: bool, produce_date: date | None = None) -> dict[str, Any]:
    """Плечо запроса: откуда забираем или куда везём.

    Порядок предпочтений — от точного к приблизительному. Терминал точнее
    города, код КЛАДР точнее строки адреса, строка адреса точнее названия
    города: одноимённых населённых пунктов в России десятки.
    """
    point: dict[str, Any] = {"variant": "address" if to_address else "terminal"}
    if produce_date is not None:
        point["produceDate"] = produce_date.isoformat()

    if not to_address and party.terminal_code:
        point["terminalID"] = party.terminal_code
        return point
    if party.carrier_city_code:
        # У Деловых Линий город — код КЛАДР, а не ФИАС. Сопоставление ведёт
        # ``directories`` через ``city_carrier_map`` (ADR-0009).
        point["city"] = party.carrier_city_code
    if to_address:
        point["address"] = {"search": party.address or party.city_name}
    elif "city" not in point:
        point["address"] = {"search": party.city_name}
    return point


def build_quote_payload(req: QuoteRequest, delivery_type: str) -> dict[str, Any]:
    """Тело ``POST /v2/calculator`` для одного вида перевозки.

    ``appkey`` и ``sessionID`` подставляет клиент: они одинаковы для всех
    вызовов учётной записи и в предметную часть запроса не входят.
    """
    return {
        "delivery": {
            "deliveryType": {"type": delivery_type},
            "derival": _point(
                req.sender, to_address=req.pickup, produce_date=req.required_delivery_date
            ),
            "arrival": _point(req.recipient, to_address=req.delivery_to_door),
        },
        "cargo": cargo_block(req.places, req.declared_value, insure=req.insurance),
    }


def _promised_dates(order_dates: object, *, to_door: bool) -> tuple[date | None, date | None]:
    if not isinstance(order_dates, dict):
        return None, None
    fields = _DOOR_DATE_FIELDS if to_door else _TERMINAL_DATE_FIELDS
    first = parse_carrier_date(order_dates.get(fields[0]))
    last = parse_carrier_date(order_dates.get(fields[1])) or first
    if first is None:
        # Перевозчик заполняет плечи по-разному: при выдаче на терминале
        # адресных дат нет и наоборот. Второй набор — не догадка, а прямая
        # проверка соседних полей того же объекта.
        other = _TERMINAL_DATE_FIELDS if to_door else _DOOR_DATE_FIELDS
        first = parse_carrier_date(order_dates.get(other[0]))
        last = parse_carrier_date(order_dates.get(other[1])) or first
    return first, last


def _transit_days(pickup: date | None, promised: date | None) -> int:
    """Срок в днях от передачи груза до вручения.

    Без обеих дат срок неизвестен, и это ноль, а не выдумка: ранжирование
    по сроку в таком случае опирается на ``promised_delivery_date``, которой
    тоже нет, и предложение честно выглядит хуже датированных.
    """
    if pickup is None or promised is None:
        return 0
    return max((promised - pickup).days, 0)


def parse_quote(
    body: dict[str, Any],
    *,
    delivery_type: str,
    to_door: bool,
    price_source: PriceSource,
) -> Quote:
    """Ответ расчёта → котировка.

    Поднимает ``ContractPriceError``, если цена договорная: см. модульную
    строку документации.
    """
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("в ответе расчёта Деловых Линий нет объекта data")

    price = money_from_response(data.get("price"))
    if price is None:
        # Флаг стоит на плечах, а не на корне ответа, поэтому договорной
        # считается перевозка, у которой нет итоговой цены и хотя бы одно
        # плечо помечено договорным.
        if _has_contract_price(data):
            raise ContractPriceError(delivery_type)
        raise ValueError("Деловые Линии не вернули стоимость перевозки")

    order_dates = data.get("orderDates")
    promised, promised_max = _promised_dates(order_dates, to_door=to_door)
    pickup_date = (
        parse_carrier_date(order_dates.get("pickup")) if isinstance(order_dates, dict) else None
    )

    breakdown = {}
    for key, label in (("derival", "pickup"), ("arrival", "delivery"), ("insurance", "insurance")):
        leg = data.get(key)
        amount = (
            money_from_response(leg.get("price"))
            if isinstance(leg, dict)
            else money_from_response(leg)
        )
        if amount is not None:
            breakdown[label] = amount

    return Quote(
        service_code=delivery_type,
        tariff_code=delivery_type,
        service_name=_SERVICE_NAMES.get(delivery_type, delivery_type),
        price=price,
        transit_days_min=_transit_days(pickup_date, promised),
        transit_days_max=_transit_days(pickup_date, promised_max),
        promised_delivery_date=promised_max or promised,
        price_source=price_source,
        raw=data,
        price_breakdown=breakdown,
    )


def _has_contract_price(data: dict[str, Any]) -> bool:
    for key in ("intercity", "derival", "arrival", "small", "air", "express", "letter"):
        leg = data.get(key)
        if isinstance(leg, dict) and leg.get("contractPrice") is True:
            return True
    return False


#: Названия видов перевозки для выдачи. Интерфейс русский (CLAUDE.md §6).
_SERVICE_NAMES: Final[dict[str, str]] = {
    "auto": "Автодоставка",
    "express": "Экспресс-перевозка",
    "small": "Малогабаритный груз",
    "letter": "Доставка документов",
    "avia": "Авиаперевозка",
}
