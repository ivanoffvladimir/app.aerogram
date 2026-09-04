"""Заказы ПЭК: оформление, поиск, история статусов, печатные формы.

Сверено по официальной документации перевозчика (ADR-0020) и по девяти
официальным примерам запросов `preregistration`, которые он публикует
на той же странице.

Пять особенностей контракта, каждая из которых меняет реализацию.

**Время без смещения, но не UTC.** Документация ``/cargos/statusfullhistory/``
говорит прямо: «Все время в часовом поясе **UTC + 3 часа**», а сами метки
приходят строкой без смещения (``"2022-09-26T17:42:37"``). Прочитать их как
UTC значит сдвинуть всю ленту на три часа: события встанут не в том порядке
относительно наших собственных отметок, а срок доставки посчитается неверно.

**Отменённые статусы остаются в ленте.** У события есть признак ``isCancel`` —
«статус был выставлен, а позднее отменён». Такое событие не выбрасывается,
но и не считается наступившим: оно помечается в комментарии, а нормализатор
статуса его не увидит.

**Заявка возвращает код груза сразу.** ``preregistration/submit`` отдаёт
``documentId`` (номер заявки) и массив ``cargos`` с ``cargoCode`` — и именно
код груза служит идентификатором во всех остальных методах. Поэтому,
в отличие от Деловых Линий, результат создания **не** ``is_pending``.

**Поиск по нашему номеру — перебор по окну дат.** Метода «найти по номеру
клиента» у ПЭК нет. Есть ``/cargos/listallorderbylogin/``, который отдаёт
грузы за период с полем ``orderNumber``. Для сверки «призраков» этого
достаточно: мы ищем заказ, который только что пытались создать, поэтому окно
узкое и выбирается по дате подачи заявки (``selectBy: 1``).

**В документации перепутаны алфавиты.** Ответ ``listallorderbylogin``
напечатан как ``"сargos"`` и ``"сode"`` — с **кириллической «с»**. Опечатка
это в документации или в самом API, по бумаге не понять, поэтому разбираются
оба написания.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final

from aerogram.carriers.base import (
    Party,
    RawEvent,
    ShipmentRequest,
    ShipmentResult,
)
from aerogram.carriers.pecom.mapping import (
    DEFAULT_TARIFF_TYPES,
    TARIFF_NAMES,
    as_number,
    declared_value_rub,
    to_metres,
    volume_m3,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.logging import get_logger

__all__ = [
    "CANCELLATION_PATH",
    "LIST_ORDERS_PATH",
    "PECOM_TZ",
    "PRINT_PATH",
    "SEARCH_WINDOW_DAYS",
    "STATUS_HISTORY_PATH",
    "SUBMIT_PATH",
    "create_payload",
    "list_orders_payload",
    "parse_created",
    "parse_found",
    "parse_printable",
    "parse_statuses",
    "print_payload",
    "status_history_payload",
]

log = get_logger(__name__)

SUBMIT_PATH: Final = "/preregistration/submit/"
STATUS_HISTORY_PATH: Final = "/cargos/statusfullhistory/"
LIST_ORDERS_PATH: Final = "/cargos/listallorderbylogin/"
PRINT_PATH: Final = "/order/print/"
CANCELLATION_PATH: Final = "/order/cancellation/"

#: «Все время в часовом поясе UTC + 3 часа» — прямая цитата из документации
#: метода истории статусов. Смещения в самих метках нет.
PECOM_TZ: Final = timezone(timedelta(hours=3), "UTC+3")

#: Окно поиска для сверки «призраков», в днях. Сверка ищет заказ, который
#: платформа только что пыталась создать, поэтому окно узкое: широкое
#: означало бы выкачивать журнал за месяцы ради одной строки.
SEARCH_WINDOW_DAYS: Final = 7

#: Выборка по дате подачи заявки: «0 — по дате приемки груза на склад ПЭК,
#: 1 — по дате подачи заявки, 2 — по дате забора груза».
_SELECT_BY_ORDER_DATE: Final = 1

#: Тип печатной формы: «big» — заявка, «simple» — этикетка груза.
PRINT_TYPE_LABEL: Final = "simple"

#: Способ передачи груза в ПЭК: 0 — самопривоз на склад, 3 — забор
#: от отправителя («На машину и перевозку»).
_ORDER_TYPE_SELF_DELIVERY: Final = 0
_ORDER_TYPE_PICKUP: Final = 3

#: Тип контрагента: 1 — юридическое лицо, 2 — ИП, 3 — физическое лицо.
_LEGAL_ENTITY: Final = 1
_INDIVIDUAL: Final = 3

#: Плательщик: 1 — отправитель, 2 — получатель, 3 — третье лицо.
_PAYER_SENDER: Final = 1

#: Точность габаритов, весов и объёма в теле запроса. Деньги этим путём
#: не ходят — см. ``mapping.as_number``.
_ZERO: Final = Decimal(0)
_METRE: Final = Decimal("0.001")
_KG: Final = Decimal("0.001")
_M3: Final = Decimal("0.000001")


def _counterparty(party: Party, *, to_address: bool) -> dict[str, Any]:
    """Отправитель или получатель в терминах ПЭК.

    Юридическое лицо отличается от физического наличием ИНН: у первого он
    обязателен, у второго вместо него передаётся блок ``individual``.
    Угадывать здесь нечего — ИНН либо есть в карточке контрагента, либо нет.
    """
    side: dict[str, Any] = {}
    warehouse = party.terminal_code or party.carrier_city_code
    if to_address:
        side["addressStock"] = party.address or party.city_name
    elif warehouse:
        side["warehouseId"] = warehouse

    if party.inn:
        side["legalForm"] = _LEGAL_ENTITY
        side["inn"] = party.inn
        side["title"] = party.name or party.city_name
    else:
        side["legalForm"] = _INDIVIDUAL
        side["title"] = party.name or party.contact_person or ""
        side["individual"] = _individual_name(party.name or party.contact_person or "")

    if party.contact_person:
        side["person"] = party.contact_person
    if party.phone:
        side["personPhones"] = [{"phone": party.phone}]
    return side


def _individual_name(full_name: str) -> dict[str, str]:
    """ФИО физического лица по строке «Фамилия Имя Отчество».

    Разбор по пробелам — не догадка о значении полей, а единственный способ
    заполнить обязательный для физлиц блок из того, что у нас есть. Пустые
    части не выдумываются.
    """
    parts = [p for p in full_name.split() if p]
    name: dict[str, str] = {}
    if parts:
        name["lastName"] = parts[0]
    if len(parts) > 1:
        name["firstName"] = parts[1]
    if len(parts) > 2:
        name["patronymic"] = parts[2]
    return name


def create_payload(req: ShipmentRequest, *, description: str) -> dict[str, Any]:
    """Тело ``POST /preregistration/submit/``.

    Наш номер уходит в ``orderNumber`` и в ``customerCorrelation``
    («произвольное значение для синхронизации на стороне клиента») — по
    первому заказ потом находится сверкой «призраков» (FR-2.5).
    """
    if not req.places:
        raise ValueError("оформление без грузовых мест невозможно")

    tariff_raw = req.extras.get("tariff_type")
    tariff = DEFAULT_TARIFF_TYPES[0]
    if isinstance(tariff_raw, int | str):
        try:
            candidate = int(tariff_raw)
        except ValueError:
            candidate = tariff
        if candidate in TARIFF_NAMES:
            tariff = candidate

    common: dict[str, Any] = {
        "customerCorrelation": req.number,
        "orderNumber": req.number,
        "type": tariff,
        "description": description,
        "positionsCount": len(req.places),
        # Габариты — максимум по местам, вес и объём — суммы: так в примерах
        # перевозчика, где на весь груз передаётся один набор.
        "length": as_number(to_metres(max(p.length_cm for p in req.places)), _METRE),
        "width": as_number(to_metres(max(p.width_cm for p in req.places)), _METRE),
        "height": as_number(to_metres(max(p.height_cm for p in req.places)), _METRE),
        "weight": as_number(sum((p.weight_kg for p in req.places), start=_ZERO), _KG),
        "volume": as_number(sum((volume_m3(p) for p in req.places), start=_ZERO), _M3),
    }

    services: dict[str, Any] = {
        "transporting": {"payer": {"type": _PAYER_SENDER}},
        "delivery": {"enabled": req.delivery_to_door},
        "insurance": {"enabled": req.insurance},
    }
    if req.delivery_to_door:
        services["delivery"]["payer"] = {"type": _PAYER_SENDER}
    if req.insurance:
        services["insurance"]["cost"] = declared_value_rub(req.declared_value)
        services["insurance"]["payer"] = {"type": _PAYER_SENDER}

    sender = _counterparty(req.sender, to_address=req.pickup)
    sender["orderType"] = _ORDER_TYPE_PICKUP if req.pickup else _ORDER_TYPE_SELF_DELIVERY
    if req.pickup:
        # «Планируемая дата передачи груза в ПЭК. Обязательный для orderType 3.»
        sender["plannedDate"] = utcnow().date().isoformat()

    return {
        "sender": sender,
        "cargos": [
            {
                "common": common,
                "receiver": _counterparty(req.recipient, to_address=req.delivery_to_door),
                "services": services,
            }
        ],
    }


def parse_created(body: dict[str, Any], *, number: str) -> ShipmentResult:
    """Ответ оформления → результат создания.

    ``cargoCode`` служит идентификатором груза во всех остальных методах ПЭК,
    и приходит он сразу. Поэтому ``is_pending`` здесь не ставится.
    """
    cargos = body.get("cargos")
    if not isinstance(cargos, list) or not cargos:
        raise ValueError("ПЭК не вернул ни одного груза в ответе на оформление")
    first = cargos[0]
    if not isinstance(first, dict):
        raise ValueError("неожиданный формат груза в ответе ПЭК")

    cargo_code = first.get("cargoCode")
    if not cargo_code:
        raise ValueError("ПЭК не вернул код груза")

    # В документации ключ штрих-кода напечатан как "barсode" — с кириллической
    # «с». Читаем оба написания, чтобы не потерять штрих-код на опечатке.
    barcode = first.get("barcode") or first.get("barсode")
    return ShipmentResult(
        external_id=str(cargo_code),
        tracking_number=str(barcode or cargo_code),
        promised_delivery_date=None,
        price_actual=None,
        is_pending=False,
        raw={"documentId": body.get("documentId"), "cargo": first, "number": number},
    )


def list_orders_payload(
    *, today: date | None = None, days: int = SEARCH_WINDOW_DAYS
) -> dict[str, Any]:
    """Тело ``POST /cargos/listallorderbylogin/`` за узкое окно дат."""
    end = today or utcnow().date()
    return {
        "selectBy": _SELECT_BY_ORDER_DATE,
        "dateBegin": (end - timedelta(days=days)).isoformat(),
        "dateEnd": end.isoformat(),
    }


def _cargo_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Строки журнала. Ключ читается в обоих алфавитах — см. модуль."""
    rows = body.get("cargos")
    if not isinstance(rows, list):
        rows = body.get("сargos")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def parse_found(body: dict[str, Any], *, number: str) -> ShipmentResult | None:
    """Найти в журнале заказ с нашим номером.

    ``None`` — заказа нет. Для сверки «призраков» это значимый ответ: он
    означает, что создание действительно не прошло.
    """
    for row in _cargo_rows(body):
        if str(row.get("orderNumber") or "") != number:
            continue
        code = row.get("code") or row.get("сode")
        if not code:
            continue
        return ShipmentResult(
            external_id=str(code),
            tracking_number=str(code),
            promised_delivery_date=None,
            price_actual=None,
            is_pending=False,
            raw=row,
        )
    return None


def status_history_payload(cargo_codes: tuple[str, ...]) -> dict[str, Any]:
    """Тело ``POST /cargos/statusfullhistory/``."""
    if not cargo_codes:
        raise ValueError("история статусов без кода груза невозможна")
    return {"cargoCodes": list(cargo_codes)}


def _parse_event_time(value: object) -> datetime | None:
    """Метка времени ПЭК: без смещения в строке, но в зоне UTC+3.

    Приписать ей UTC значило бы сдвинуть всю ленту на три часа.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PECOM_TZ)
    return parsed.astimezone(UTC)


def parse_statuses(body: dict[str, Any]) -> list[RawEvent]:
    """История статусов → лента событий.

    Ответ приходит массивом по грузам, поэтому клиент оборачивает его
    в ``{"items": [...]}``.
    """
    items = body.get("items")
    if not isinstance(items, list):
        items = [body]

    events: list[RawEvent] = []
    for cargo in items:
        if not isinstance(cargo, dict):
            continue
        statuses = cargo.get("statuses")
        if not isinstance(statuses, list):
            continue
        for row in statuses:
            if not isinstance(row, dict):
                continue
            occurred_at = _parse_event_time(row.get("date"))
            name = row.get("name")
            if occurred_at is None or not name:
                log.warning("pecom.status_row_incomplete", cargo=str(cargo.get("cargoCode")))
                continue
            cancelled = row.get("isCancel") is True
            events.append(
                RawEvent(
                    occurred_at=occurred_at,
                    # Отменённый статус не должен нормализоваться как
                    # наступивший: он остаётся в ленте пометкой.
                    status_raw="ОТМЕНЁННЫЙ СТАТУС" if cancelled else str(name),
                    city=None,
                    comment=f"{name} (отменён)" if cancelled else str(name),
                    raw=row,
                )
            )
    events.sort(key=lambda e: e.occurred_at)
    return events


def print_payload(cargo_code: str, *, form: str = PRINT_TYPE_LABEL) -> dict[str, Any]:
    """Тело ``POST /order/print/``."""
    return {"cargoIndex": cargo_code, "type": form}


def parse_printable(body: object) -> bytes | None:
    """Печатная форма → байты PDF.

    Формат ответа в документации записан неоднозначно — ``{ "JVBERi0xLjQKJe..." }``,
    что не является корректным JSON. Поэтому принимаются все правдоподобные
    формы: голая строка, объект с единственным значением, объект с ключом
    вроде ``file`` или ``content``. Ничего похожего на base64 — ``None``,
    а не мусор в файле.
    """
    candidates: list[str] = []
    if isinstance(body, str):
        candidates.append(body)
    elif isinstance(body, dict):
        for key in ("file", "content", "data", "pdf"):
            value = body.get(key)
            if isinstance(value, str):
                candidates.append(value)
        for key, value in body.items():
            if isinstance(value, str):
                candidates.append(value)
            elif value is None and isinstance(key, str) and len(key) > 40:
                # Документация показывает base64 в позиции ключа.
                candidates.append(key)

    for encoded in candidates:
        text = encoded.strip()
        if len(text) < 8:
            continue
        try:
            decoded = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            continue
        if decoded[:4] == b"%PDF":
            return decoded
    log.warning("pecom.printable_unreadable")
    return None
