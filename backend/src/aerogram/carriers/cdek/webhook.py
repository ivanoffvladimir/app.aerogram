"""Разбор входящего вебхука СДЭК. Без ввода-вывода, как и ``mapping``.

**Источник формата.** Официальный SDK покрывает только ПОДПИСКУ на вебхуки
(`setWebhooks`, типы событий `ORDER_STATUS` и `PRINT_FORM`); состава тела
в нём нет. Форма тела взята из типов стороннего клиента
``shevernitskiy/cdek`` (`src/types/api/webhook.ts`), то есть источник — не
официальный, и по планке ADR-0010 он показывает форму, но подтверждением
не является. Сверка с боевым контуром — обязательный пункт при подключении.

**Ловушка, ради которой этот модуль отдельный и покрыт тестами.**
В ``attributes`` есть два похожих поля:

* ``status_code`` — легаси, число вроде ``"3"``;
* ``code`` — строковый статус (``DELIVERED``, ``NOT_DELIVERED``, …), тот самый
  словарь, что лежит в ``status_map/cdek.yaml``.

Взять первое вместо второго — значит отправлять числа в нормализатор
статусов. Он не роняет обработку, а помечает событие несопоставленным
и ставит ``IN_TRANSIT``: лента наполнится, доставка не отметится никогда,
и заметят это на разборе просрочек, а не в тестах.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aerogram.carriers.base import RawEvent, WebhookUpdate
from aerogram.shared.clock import ensure_utc
from aerogram.shared.logging import get_logger

__all__ = ["EVENT_ORDER_STATUS", "parse_order_status"]

log = get_logger(__name__)

#: Единственный тип события, несущий статус заказа. ``PRINT_FORM`` сообщает
#: о готовности печатной формы и к ленте трекинга отношения не имеет.
EVENT_ORDER_STATUS = "ORDER_STATUS"


def parse_order_status(payload: dict[str, Any]) -> list[WebhookUpdate]:
    """Разобрать тело вебхука в обновления по заказам.

    Пустой список — это «принять и ничего не делать», и он возвращается
    осознанно: чужой тип события или тело без идентификатора заказа не повод
    отвечать перевозчику ошибкой. Ошибка заставила бы его повторять доставку
    события, которое нам всё равно не нужно.
    """
    if payload.get("type") != EVENT_ORDER_STATUS:
        return []

    # ``uuid`` — идентификатор СУЩНОСТИ (заказа), а не события. Именно он
    # известен нам как ``external_id``: возвратный заказ у СДЭК свой,
    # со своим uuid, и его события не приклеятся к прямому отправлению.
    external_id = _text(payload.get("uuid"))
    if not external_id:
        log.warning("cdek.webhook_without_order_id", event_type=EVENT_ORDER_STATUS)
        return []

    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        log.warning("cdek.webhook_without_attributes", external_id=external_id)
        return []

    status = _text(attributes.get("code"))
    if not status:
        # Без статуса событие пустое: записать его значило бы засорить ленту
        # строкой, о которой нечего сказать.
        log.warning("cdek.webhook_without_status", external_id=external_id)
        return [WebhookUpdate(external_id=external_id)]

    occurred_at = _moment(attributes.get("status_date_time")) or _moment(payload.get("date_time"))
    if occurred_at is None:
        log.warning("cdek.webhook_without_time", external_id=external_id, status=status)
        return [WebhookUpdate(external_id=external_id)]

    event = RawEvent(
        occurred_at=occurred_at,
        status_raw=status,
        city=_text(attributes.get("city_name")) or None,
        # Сырые атрибуты нужны разбору спорных ситуаций: причина отказа
        # в доставке живёт в ``status_reason_code``, и без неё разговор
        # с перевозчиком начинается с «а что случилось».
        raw={str(k): v for k, v in attributes.items()},
    )
    return [WebhookUpdate(external_id=external_id, events=(event,))]


def _text(value: object) -> str:
    """Строка или пусто. Числа и ``None`` в идентификаторы не превращаются."""
    return value.strip() if isinstance(value, str) else ""


def _moment(value: object) -> datetime | None:
    """Момент события. СДЭК присылает смещение без двоеточия: ``+0700``.

    Наивное время трактуется как UTC — это трактовка ``shared.clock``,
    и здесь она не переопределяется.
    """
    text = _text(value)
    if not text:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        log.warning("cdek.webhook_bad_time", value=text)
        return None
