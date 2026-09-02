"""Заказы СДЭК: тело запроса и разбор ответов. Без ввода-вывода, как ``mapping``.

Контур сверен по исходникам официального SDK СДЭК для API 2.0 (ADR-0010):
сущность ``Order`` (``CommonTrait``, ``OrderTrait``, ``TariffTrait``),
вложенные ``Contact``/``Phone``/``Package``/``Item``/``Location``/``Services``,
ответы ``EntityResponse`` (``entity`` + ``requests``) и ``OrderResponse``
(``statuses[]``, ``cdek_number``, ``delivery_detail``).

Что важно и что легко перепутать:

* **Единицы.** Вес — в **граммах**, габариты — в **сантиметрах**. Домен
  работает в килограммах, перевод делает этот модуль, и делает его через
  ``grams_from_kg`` — ту же функцию, что и расчёт, чтобы заказ ушёл с тем
  весом, по которому считали цену.
* **Идентификаторы.** ``entity.uuid`` — идентификатор заказа в СДЭК, наш
  ``external_id``. ``cdek_number`` — номер накладной, наш ``tracking_number``;
  в ответе на создание его **нет**, он присваивается позже. ``number`` —
  наш собственный номер, по нему заказ ищется при сверке «призраков».
* **Ошибки.** Отказ живёт в ``requests[0]``: ``state == "INVALID"`` и
  ``errors[]``. Код HTTP при этом бывает и 200 — SDK проверяет тело,
  а не статус, и мы делаем так же.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aerogram.carriers.base import CancelResult, RawEvent, ShipmentRequest, ShipmentResult
from aerogram.carriers.cdek.mapping import CDEK_TYPE_DELIVERY, grams_from_kg
from aerogram.shared.clock import ensure_utc
from aerogram.shared.logging import get_logger

__all__ = [
    "NOT_FOUND_CODES",
    "REQUEST_INVALID",
    "cancel_result",
    "order_payload",
    "parse_order",
    "parse_statuses",
    "request_error",
]

log = get_logger(__name__)

#: Состояние заявки, означающее отказ. Остальные (``ACCEPTED``, ``WAITING``,
#: ``SUCCESSFUL``) — стадии обработки, и заказ при них существует.
REQUEST_INVALID = "INVALID"

#: Коды ошибок «сущности нет» из словаря SDK. Только они превращают поиск
#: в «не найден»; любая другая ошибка остаётся ошибкой. Направление ошибки
#: здесь дорогое: ложное «не найден» на сверке «призраков» означает второй
#: заказ у перевозчика — с оплатой и вторым грузом.
NOT_FOUND_CODES: frozenset[str] = frozenset(
    {"v2_entity_not_found", "v2_order_not_found", "v2_entity_not_found_im_number"}
)

#: Код дополнительной услуги страхования (``Constants::SERVICE_CODES``).
#: Параметр — объявленная стоимость в рублях.
SERVICE_INSURANCE = "INSURANCE"


def order_payload(req: ShipmentRequest) -> dict[str, Any]:
    """Тело ``POST orders`` по сущности ``Order`` SDK."""
    payload: dict[str, Any] = {
        # Тип 2 — «доставка»: договор клиента, B2B-отправитель. Тип 1 —
        # интернет-магазин с продавцом и оплатой при получении, это не наш случай.
        "type": CDEK_TYPE_DELIVERY,
        "number": req.number,
        "tariff_code": int(req.tariff_code),
        "from_location": _location(req.sender),
        "to_location": _location(req.recipient),
        "sender": _contact(req.sender),
        "recipient": _contact(req.recipient),
        "packages": [
            {
                "number": str(index),
                "weight": grams_from_kg(place.weight_kg),
                "length": place.length_cm,
                "width": place.width_cm,
                "height": place.height_cm,
            }
            for index, place in enumerate(req.places, start=1)
        ],
    }
    if req.comment:
        payload["comment"] = req.comment
    if req.sender.terminal_code:
        payload["shipment_point"] = req.sender.terminal_code
    if req.recipient.terminal_code:
        payload["delivery_point"] = req.recipient.terminal_code
    if req.insurance:
        # Параметр услуги — сумма в рублях. Деньги приходят в копейках,
        # и здесь единственное место, где они превращаются в рубли: строкой,
        # через Decimal, чтобы 480000.50 не стало 480000.4999.
        payload["services"] = [
            {
                "code": SERVICE_INSURANCE,
                "parameter": str(req.declared_value.to_major()),
            }
        ]
    return payload


def _location(party: Any) -> dict[str, Any]:
    """Пункт заказа. Код города СДЭК точнее индекса и названия (см. ``quote``)."""
    location: dict[str, Any] = {}
    if party.carrier_city_code:
        location["code"] = int(party.carrier_city_code)
    elif party.postal_code:
        location["postal_code"] = party.postal_code
    else:
        location["country_code"] = "RU"
        location["city"] = party.city_name
    if party.address:
        location["address"] = party.address
    return location


def _contact(party: Any) -> dict[str, Any]:
    """Контакт заказа. Обязательны имя и телефон — без них СДЭК отказывает."""
    contact: dict[str, Any] = {"name": party.contact_person or party.name or ""}
    if party.name and party.contact_person:
        contact["company"] = party.name
    if party.phone:
        contact["phones"] = [{"number": party.phone}]
    if party.email:
        contact["email"] = party.email
    return contact


def request_error(body: dict[str, Any]) -> tuple[str, str] | None:
    """Код и сообщение отказа из ``requests[0]``, если заявка отклонена."""
    requests = body.get("requests")
    first = requests[0] if isinstance(requests, list) and requests else None
    if not isinstance(first, dict):
        return _top_level_error(body)
    if first.get("state") != REQUEST_INVALID and not first.get("errors"):
        return None
    return _first_error(first.get("errors")) or ("unknown", "СДЭК отклонил заявку")


def _top_level_error(body: dict[str, Any]) -> tuple[str, str] | None:
    """Ошибка без заявки — так СДЭК отвечает на неверный запрос целиком."""
    return _first_error(body.get("errors"))


def _first_error(errors: object) -> tuple[str, str] | None:
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0] if isinstance(errors[0], dict) else {}
    return str(first.get("code") or "unknown"), str(first.get("message") or "")


def parse_order(body: dict[str, Any]) -> ShipmentResult | None:
    """``OrderResponse`` → результат создания. ``None`` — заказа нет.

    Ответ на создание несёт только ``entity.uuid``: номер накладной СДЭК
    присваивает позже, и до него заказ считается принятым, а не созданным —
    ``is_pending``. Домен ставит по нему ``ACCEPTED`` и опрашивает дальше.
    """
    entity = body.get("entity")
    if not isinstance(entity, dict) or not entity.get("uuid"):
        return None
    cdek_number = entity.get("cdek_number")
    return ShipmentResult(
        external_id=str(entity["uuid"]),
        tracking_number=str(cdek_number) if cdek_number else None,
        promised_delivery_date=None,
        price_actual=None,
        is_pending=not cdek_number,
        raw={
            "cdek_number": cdek_number,
            "requests": body.get("requests"),
            "delivery_detail": entity.get("delivery_detail"),
        },
    )


def parse_statuses(body: dict[str, Any]) -> list[RawEvent]:
    """``entity.statuses[]`` → сырые события ленты.

    Статус — ``code`` (``DELIVERED``, ``NOT_DELIVERED``, …), тот же словарь,
    что и в вебхуке и в ``status_map/cdek.yaml``. Удалённые статусы
    (``deleted``) пропускаются: СДЭК так отзывает ошибочно проставленные.
    """
    entity = body.get("entity")
    statuses = entity.get("statuses") if isinstance(entity, dict) else None
    events: list[RawEvent] = []
    for row in statuses or []:
        if not isinstance(row, dict) or row.get("deleted"):
            continue
        code = row.get("code")
        moment = _moment(row.get("date_time"))
        if not code or moment is None:
            log.warning("cdek.status_row_incomplete", code=code)
            continue
        events.append(
            RawEvent(
                occurred_at=moment,
                status_raw=str(code),
                city=str(row["city"]) if row.get("city") else None,
                comment=str(row["name"]) if row.get("name") else None,
                raw={"reason_code": row.get("reason_code"), "name": row.get("name")},
            )
        )
    return events


def cancel_result(body: dict[str, Any]) -> CancelResult:
    """Ответ на ``POST orders/{uuid}/refusal``."""
    error = request_error(body)
    if error is not None:
        return CancelResult(accepted=False, message=error[1] or None, raw=body)
    return CancelResult(accepted=True, raw=body)


def _moment(value: object) -> datetime | None:
    """Время статуса. СДЭК присылает смещение без двоеточия: ``+0700``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.strip()))
    except ValueError:
        log.warning("cdek.status_bad_time", value=value)
        return None
