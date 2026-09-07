"""Печатные формы СДЭК: сборка запроса и разбор ответа. Без ввода-вывода.

**Источник контракта** — исходники официального SDK СДЭК для API 2.0
(``cdek-it/sdk2.0``, планка ADR-0010), файлы ``src/Actions/Barcodes.php``,
``src/BaseTypes/Barcode.php``, ``src/BaseTypes/OrdersList.php``,
``src/Dto/Response.php``, ``src/Dto/Request.php``.

Оттуда дословно:

* путь ``/print/barcodes`` — «запрос на формирование ШК-места к заказу»;
* тело: ``orders[]`` (в каждом ``order_uuid`` **или** ``cdek_number``),
  ``copy_count`` (по умолчанию 1), ``format`` — ``A4``, ``A5`` или ``A6``
  (по умолчанию ``A4``);
* ответ — общий конверт ``{"entity": …, "requests": […]}``;
* ``entity`` печатной формы несёт ``uuid``, ``url`` («ссылка на скачивание
  файла»), ``orders[]`` и ``statuses[]``;
* ``requests[].state`` принимает ``ACCEPTED``, ``WAITING``, ``SUCCESSFUL``,
  ``INVALID``;
* файл забирается тем же путём с суффиксом ``.pdf``:
  ``download()`` в SDK — это ``get(slug(uuid) . '.pdf')``.

**Почему ШК-место, а не квитанция.** У СДЭК два разных документа: квитанция
(``/print/orders``) и ШК-место (``/print/barcodes``). Этикетка — то, что
клеится на коробку, — это ШК-место, и только у него есть выбор листа
``A4/A5/A6``, ровно тот, что объявлен в ``Capabilities`` адаптера.
Квитанция листа не выбирает и печатается в двух экземплярах: это документ
для курьера, а не для склада.
"""

from __future__ import annotations

from typing import Any, Final

from aerogram.shared.enums import LabelFormat

__all__ = [
    "BARCODES_PATH",
    "FORMAT_CODES",
    "barcode_payload",
    "download_path",
    "form_url",
    "is_ready",
    "print_uuid",
    "status_path",
    "waybill_number",
]

BARCODES_PATH: Final = "/print/barcodes"

#: Лист печати. ZPL здесь нет намеренно: СДЭК его не предлагает,
#: и подставить вместо него PDF значило бы отдать на термопринтер файл,
#: который тот не напечатает.
FORMAT_CODES: Final[dict[LabelFormat, str]] = {
    LabelFormat.PDF_A4: "A4",
    LabelFormat.PDF_A5: "A5",
    LabelFormat.PDF_A6: "A6",
}

#: Состояние заявки, при котором форма готова (``Dto/Request::state``).
STATE_SUCCESSFUL: Final = "SUCCESSFUL"


def barcode_payload(order_uuid: str, fmt: LabelFormat, *, copies: int = 1) -> dict[str, Any]:
    """Тело запроса на формирование ШК-места.

    Заказ адресуется по ``order_uuid``, а не по ``cdek_number``: номер СДЭК
    появляется не сразу после создания, а идентификатор заказа известен
    всегда — это наш ``external_id``.
    """
    return {
        "orders": [{"order_uuid": order_uuid}],
        "copy_count": copies,
        "format": FORMAT_CODES[fmt],
    }


def status_path(print_uuid_: str) -> str:
    """Путь опроса готовности печатной формы."""
    return f"{BARCODES_PATH}/{print_uuid_}"


def download_path(print_uuid_: str) -> str:
    """Путь самого файла. Суффикс ``.pdf`` — как в ``Barcodes::download``."""
    return f"{BARCODES_PATH}/{print_uuid_}.pdf"


def print_uuid(body: dict[str, Any]) -> str | None:
    """Идентификатор запроса на печать из ``entity.uuid``."""
    entity = body.get("entity")
    if not isinstance(entity, dict):
        return None
    value = entity.get("uuid")
    return str(value) if value else None


def is_ready(body: dict[str, Any]) -> bool:
    """Готова ли форма к скачиванию.

    Два независимых признака, и достаточно любого: состояние заявки
    ``SUCCESSFUL`` и присутствие ``entity.url``. Словарь ``statuses[]``
    самой формы сюда не берётся сознательно — состава его кодов
    в источнике нет, а гадать о значении статуса значит однажды
    объявить готовым то, чего ещё нет.
    """
    if form_url(body):
        return True
    requests = body.get("requests")
    if not isinstance(requests, list):
        return False
    return any(
        isinstance(item, dict) and item.get("state") == STATE_SUCCESSFUL for item in requests
    )


def form_url(body: dict[str, Any]) -> str | None:
    """``entity.url`` — ссылка на файл, если СДЭК её уже проставил."""
    entity = body.get("entity")
    if not isinstance(entity, dict):
        return None
    value = entity.get("url")
    return str(value) if value else None


def waybill_number(body: dict[str, Any]) -> str | None:
    """Номер накладной СДЭК из ``entity.orders[].cdek_number``.

    Читается по случаю, а не требуется: ``OrdersList`` несёт это поле,
    но заполненным оно приходит не всегда. Пустое значение — не ошибка,
    просто номера в этом ответе нет.

    Именно этот номер, а не идентификатор запроса на печать, и есть
    накладная (ADR-0030). Спутать их — записать в каталог накладных
    случайную строку, которая ничего не значит уже через час.
    """
    entity = body.get("entity")
    if not isinstance(entity, dict):
        return None
    orders = entity.get("orders")
    if not isinstance(orders, list):
        return None
    for order in orders:
        if not isinstance(order, dict):
            continue
        number = order.get("cdek_number")
        if number:
            return str(number)
    return None
