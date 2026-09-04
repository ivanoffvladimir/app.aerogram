"""Заказы Деловых Линий: оформление, поиск, история статусов, печатные формы.

Всё сверено по официальной OpenAPI 3.0.3 перевозчика (ADR-0020).

Четыре особенности контракта, каждая из которых меняет реализацию.

**Создаётся заявка, а не заказ.** ``POST /v2/request`` возвращает
``requestID`` — «Номер созданной заявки/предзаказа» — и, для предзаказа,
``barcode``. Номера заказа (``orderId``) в ответе нет: он появляется позже,
когда перевозчик заявку обработает. Поэтому результат создания помечается
``is_pending``, а трек-номер доезжает следующим опросом.

**Поиск по нашему номеру возможен.** ``POST /v3/orders`` принимает
``orderNumber`` — «Внутренний номер заказа клиента (например, номер заказа
интернет-магазина)». Это ровно то, что нужно сверке «призраков» (FR-2.5):
если ответ на создание не дошёл, заказ ищется по нашему собственному номеру.

**Характер груза обязателен и берётся из справочника.** ``cargo.freightUID``
объявлен обязательным, а его значения живут в справочнике перевозчика
``/v1/public/freight_types``. Выдумать UID нельзя, и подставить умолчание
тоже: перевозчик посчитает не тот груз. Поэтому он приходит из
``extras["freight_uid"]``, а без него оформление честно отказывает.

**История статусов приходит картой, а не списком.** ``statusHistory`` —
объект, ключ которого номер заказа, значение — массив событий. Так один
вызов покрывает несколько заказов.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any, Final

from aerogram.carriers.base import (
    Party,
    RawEvent,
    ShipmentRequest,
    ShipmentResult,
)
from aerogram.carriers.dellin.mapping import (
    DEFAULT_DELIVERY_TYPE,
    DELIVERY_TYPES,
    cargo_block,
    money_from_response,
    parse_carrier_date,
)
from aerogram.shared.logging import get_logger

__all__ = [
    "ORDERS_PATH",
    "PRINTABLE_PATH",
    "REQUEST_PATH",
    "STATUSES_HISTORY_PATH",
    "create_payload",
    "orders_payload",
    "parse_created",
    "parse_order",
    "parse_printable",
    "parse_statuses",
    "printable_payload",
    "statuses_payload",
]

log = get_logger(__name__)

REQUEST_PATH: Final = "/v2/request.json"
ORDERS_PATH: Final = "/v3/orders.json"
STATUSES_HISTORY_PATH: Final = "/v3/orders/statuses_history.json"
PRINTABLE_PATH: Final = "/v1/printable.json"

#: Тип печатной формы. ``order`` — накладная, единственная форма, которая
#: имеет смысл как этикетка отправления. Остальные (``bill``, ``invoice``,
#: ``giveout``) — бухгалтерия и выдача, они не про наклейку на коробку.
PRINTABLE_MODE_WAYBILL: Final = "order"

#: Тип документа заказа, в котором лежит UID накладной.
_DOC_TYPE_SHIPPING: Final = "shipping"


def _counteragent(party: Party) -> dict[str, Any]:
    """Контрагент из нашей стороны перевозки.

    Передаётся встроенным объектом, а не идентификатором адресной книги:
    книга принадлежит кабинету клиента, а у нас своя (ADR-0009). ``save``
    не ставится — заполнять чужую адресную книгу нашими данными мы не вправе.
    """
    agent: dict[str, Any] = {"name": party.name or party.city_name}
    if party.inn:
        agent["inn"] = party.inn
    return agent


def _member(party: Party) -> dict[str, Any]:
    member: dict[str, Any] = {"counteragent": _counteragent(party)}
    if party.contact_person:
        member["contactPersons"] = [{"name": party.contact_person}]
    if party.phone:
        member["phoneNumbers"] = [{"number": party.phone}]
    if party.email:
        member["email"] = party.email
    return member


def _point(party: Party, *, to_address: bool) -> dict[str, Any]:
    point: dict[str, Any] = {"variant": "address" if to_address else "terminal"}
    if not to_address and party.terminal_code:
        point["terminalID"] = party.terminal_code
        return point
    if party.carrier_city_code:
        point["city"] = party.carrier_city_code
    if to_address:
        point["address"] = {"search": party.address or party.city_name}
    elif "city" not in point:
        point["address"] = {"search": party.city_name}
    return point


def create_payload(req: ShipmentRequest, *, freight_uid: str) -> dict[str, Any]:
    """Тело ``POST /v2/request``.

    ``appkey`` и ``sessionID`` подставляет клиент. ``cargoCode`` — наш
    собственный номер: перевозчик кладёт его в «Номер ТТН клиента», и по нему
    же заказ потом ищется, что и делает сверку «призраков» возможной.
    """
    delivery_type = str(req.extras.get("delivery_type") or DEFAULT_DELIVERY_TYPE)
    if delivery_type not in DELIVERY_TYPES:
        delivery_type = DEFAULT_DELIVERY_TYPE

    cargo = cargo_block(req.places, req.declared_value, insure=req.insurance)
    cargo["freightUID"] = freight_uid

    payload: dict[str, Any] = {
        "delivery": {
            "deliveryType": {"type": delivery_type},
            "derival": _point(req.sender, to_address=req.pickup),
            "arrival": _point(req.recipient, to_address=req.delivery_to_door),
        },
        "members": {
            "requester": {"role": "sender"},
            "sender": _member(req.sender),
            "receiver": _member(req.recipient),
        },
        "cargo": cargo,
        "payment": {"type": "noncash", "primaryPayer": "sender"},
        "cargoCode": req.number,
        # Внутренний номер клиента, по которому заказ находится в журнале.
        "orderNumber": req.number,
    }
    if req.comment:
        payload["delivery"]["comment"] = req.comment
    return payload


def parse_created(body: dict[str, Any], *, number: str) -> ShipmentResult:
    """Ответ оформления → результат создания.

    ``requestID`` — номер заявки, и он же единственный идентификатор, который
    у нас есть до обработки заявки перевозчиком. Номер заказа появится позже,
    поэтому результат помечается ``is_pending``.
    """
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("в ответе оформления Деловых Линий нет объекта data")

    request_id = data.get("requestID")
    if request_id in (None, ""):
        raise ValueError("Деловые Линии не вернули номер заявки")

    barcode = data.get("barcode")
    return ShipmentResult(
        external_id=str(request_id),
        tracking_number=str(barcode) if barcode else None,
        promised_delivery_date=None,
        price_actual=None,
        # Заявка ещё не заказ: номер накладной и цена появятся после обработки.
        is_pending=True,
        raw={"data": data, "number": number},
    )


def orders_payload(*, number: str | None = None, doc_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    """Тело ``POST /v3/orders``: поиск по нашему номеру или по номерам заказов."""
    if not number and not doc_ids:
        raise ValueError("поиск заказа без номера невозможен")
    payload: dict[str, Any] = {}
    if number:
        payload["orderNumber"] = number
    if doc_ids:
        payload["docIds"] = list(doc_ids)
    return payload


def parse_order(body: dict[str, Any]) -> ShipmentResult | None:
    """Первый заказ из журнала → результат.

    ``None`` — заказа нет. Для сверки «призраков» это значимый ответ:
    он означает, что создание действительно не прошло.
    """
    orders = body.get("orders")
    if not isinstance(orders, list) or not orders:
        return None
    order = orders[0]
    if not isinstance(order, dict):
        return None

    order_id = order.get("orderId")
    if order_id in (None, ""):
        return None

    dates = order.get("orderDates")
    promised = None
    if isinstance(dates, dict):
        for field in ("derrivalToAddress", "giveoutFromOspReceiver", "arrivalToOspReceiver"):
            promised = parse_carrier_date(dates.get(field))
            if promised is not None:
                break

    return ShipmentResult(
        external_id=str(order_id),
        tracking_number=str(order_id),
        promised_delivery_date=promised,
        price_actual=money_from_response(order.get("totalSum")),
        is_pending=False,
        raw=order,
    )


def waybill_uid(body: dict[str, Any]) -> str | None:
    """UID накладной из журнала заказов.

    Печатная форма запрашивается по UID документа, а не по номеру заказа,
    поэтому его приходится доставать отдельным поиском.
    """
    orders = body.get("orders")
    if not isinstance(orders, list) or not orders:
        return None
    order = orders[0]
    documents = order.get("documents") if isinstance(order, dict) else None
    if not isinstance(documents, list):
        return None
    for doc in documents:
        if isinstance(doc, dict) and doc.get("type") == _DOC_TYPE_SHIPPING and doc.get("uid"):
            return str(doc["uid"])
    return None


def statuses_payload(doc_ids: tuple[str, ...]) -> dict[str, Any]:
    """Тело ``POST /v3/orders/statuses_history``."""
    if not doc_ids:
        raise ValueError("история статусов без номера заказа невозможна")
    return {"docIds": list(doc_ids)}


def _parse_event_time(value: object) -> datetime | None:
    """Время события. У Деловых Линий оно со смещением: ``2023-01-12T15:52:40+03:00``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        as_date = parse_carrier_date(value)
        return datetime.combine(as_date, datetime.min.time()) if as_date else None
    return parsed


def parse_statuses(body: dict[str, Any]) -> list[RawEvent]:
    """История статусов → лента событий.

    ``statusHistory`` — карта «номер заказа → массив событий», поэтому
    события всех найденных заказов сливаются в одну ленту: вызывающий
    запрашивает историю по одному отправлению.
    """
    data = body.get("data")
    history = data.get("statusHistory") if isinstance(data, dict) else None
    if not isinstance(history, dict):
        return []

    events: list[RawEvent] = []
    for order_id, rows in history.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            occurred_at = _parse_event_time(row.get("stateDate"))
            state = row.get("state")
            if occurred_at is None or not state:
                # Событие без времени или без статуса нечем ни отсортировать,
                # ни нормализовать. Пропускаем его заметно, а не молча.
                log.warning("dellin.status_row_incomplete", order_id=str(order_id))
                continue
            events.append(
                RawEvent(
                    occurred_at=occurred_at,
                    status_raw=str(state),
                    city=None,
                    comment=row.get("stateName") or row.get("detailedStatusRus") or None,
                    raw=row,
                )
            )
    events.sort(key=lambda e: e.occurred_at)
    return events


def printable_payload(doc_uid: str, *, mode: str = PRINTABLE_MODE_WAYBILL) -> dict[str, Any]:
    """Тело ``POST /v1/printable``."""
    return {"docUID": doc_uid, "mode": mode}


def parse_printable(body: dict[str, Any]) -> bytes | None:
    """Печатная форма → байты PDF.

    ``None`` — документа нет. Перевозчик отдаёт PDF строкой base64;
    испорченная строка — не повод падать, но и не повод отдать мусор.
    """
    data = body.get("data")
    if not isinstance(data, list) or not data:
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        encoded = item.get("base64")
        if not encoded:
            continue
        try:
            return base64.b64decode(str(encoded), validate=True)
        except (binascii.Error, ValueError):
            log.warning("dellin.printable_not_base64", uid=str(item.get("uid")))
            return None
    return None
