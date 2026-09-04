"""Расчёт Почты России: сборка ``/1.0/tariff`` и разбор ответа.

Здесь живут решения про деньги, поэтому они собраны в одном месте
(CLAUDE.md §7, пункт 5) — точнее, в паре с ``mapping``, где сложение НДС
и перевод единиц.

**Один запрос — одна цена.** У Почты нет выдачи списком: `POST /1.0/tariff`
считает ровно одно сочетание вида РПО, категории и вида транспортировки.
Рейт-шоппинг по ней — это N запросов, по одному на продукт, и каждый тратит
суточную квоту, привязанную к токену приложения. Поэтому набор продуктов
короткий и объявлен явно (``mapping.PRODUCTS``), а вызывающий может сузить
или расширить его через ``extras["products"]``.

**Адресация только индексом.** Ни города, ни адреса, ни кода ФИАС в теле
расчёта нет: «index-from — Почтовый индекс объекта почтовой связи места
приема», «index-to — … места назначения», обе строкой. Индекс отправления
формально можно не передавать — тогда, по документации, «Индекс ОПС точки
отправления берется из профиля клиента», — но при мультиарендности это
означало бы тихую зависимость цены от настроек чужого личного кабинета.
Поэтому передаём оба всегда, а без индекса получателя отказываемся считать:
предложение уходит в ``failures``, а не считается «неизвестно куда».

**Одно место.** РПО — одно отправление, и в теле расчёта один блок
``dimension`` и одна ``mass``. Складывать несколько мест в одно значило бы
посчитать несуществующую посылку, поэтому запрос с несколькими местами
отклоняется до вызова перевозчика.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from aerogram.carriers.base import Place, Quote, QuoteRequest
from aerogram.carriers.pochta.mapping import (
    DEFAULT_PRODUCTS,
    PochtaProduct,
    dimension_block,
    mass_grams,
    money_from_rate,
    product_by_code,
    total_price,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import PriceSource
from aerogram.shared.errors import CarrierValidationError
from aerogram.shared.logging import get_logger

__all__ = [
    "LIMIT_PATH",
    "TARIFF_PATH",
    "build_tariff_payload",
    "parse_tariff",
    "products_for",
    "require_single_place",
]

log = get_logger(__name__)

POCHTA_CODE: Final = "pochta"

TARIFF_PATH: Final = "/1.0/tariff"

#: Остаток суточной квоты. Не вызывается расчётом, но объявлен здесь:
#: квота — единственное ограничение Почты, названное в документации,
#: а её величина до получения доступов неизвестна.
LIMIT_PATH: Final = "/1.0/settings/limit"

#: Русские названия составляющих цены. Ключ — поле ответа, значение —
#: подпись в расшифровке строки выдачи (интерфейс русский, CLAUDE.md §6).
_RATE_NAMES: Final[dict[str, str]] = {
    "ground-rate": "Пересылка",
    "avia-rate": "Авиа-пересылка",
    "insurance-rate": "Объявленная ценность",
    "inventory-rate": "Опись вложения",
    "notice-rate": "Уведомление о вручении",
    "oversize-rate": "Надбавка за негабарит",
    "fragile-rate": "Отметка «Осторожно/Хрупкое»",
    "completeness-checking-rate": "Проверка комплектности",
    "contents-checking-rate": "Проверка вложения",
    "sms-notice-recipient-rate": "СМС получателю",
    "vsd-rate": "Возврат сопроводительных документов",
}


def products_for(req: QuoteRequest) -> tuple[PochtaProduct, ...]:
    """Какие продукты Почты считать.

    Вызывающий задаёт набор через ``extras["products"]``. Неизвестный код
    отбрасывается с предупреждением, а не молча: молчание здесь выглядит
    как «Почта подешевела», хотя на деле посчитали не то, что просили.
    """
    raw = req.extras.get("products")
    if not isinstance(raw, list | tuple) or not raw:
        return tuple(p for code in DEFAULT_PRODUCTS if (p := product_by_code(code)))
    wanted: list[PochtaProduct] = []
    unknown: list[str] = []
    for item in raw:
        product = product_by_code(item)
        if product is None:
            unknown.append(str(item))
        elif product not in wanted:
            wanted.append(product)
    if unknown:
        log.warning("pochta.unknown_products", requested=unknown)
    if not wanted:
        return tuple(p for code in DEFAULT_PRODUCTS if (p := product_by_code(code)))
    return tuple(wanted)


def require_single_place(req: QuoteRequest) -> Place:
    """Единственное грузовое место запроса.

    Несколько мест — не ошибка клиента вообще, а несовместимость с Почтой:
    одно РПО это одно место. Отказ до вызова, чтобы не тратить квоту.
    """
    if len(req.places) != 1:
        raise CarrierValidationError(
            "Почта России считает одно грузовое место в отправлении, "
            f"а в запросе их {len(req.places)}",
            carrier_code=POCHTA_CODE,
        )
    return req.places[0]


def _index(value: str | None, *, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise CarrierValidationError(
            f"Для расчёта по Почте России нужен почтовый индекс {what}",
            carrier_code=POCHTA_CODE,
        )
    return text


def build_tariff_payload(req: QuoteRequest, product: PochtaProduct) -> dict[str, Any]:
    """Тело ``POST /1.0/tariff`` для одного продукта.

    Обязательные по документации поля — `mail-type`, `mail-category`, `mass`,
    `inventory`, `with-order-of-notice`, `with-simple-notice` — передаются
    всегда. Три логических из них — это платные услуги, которых мы не
    заказываем, поэтому они явные ``False``, а не отсутствие поля.

    ``entries-type`` в таблице обязателен, но описан как «Категория вложения
    (Для международных отправлений)». Противоречие в самом источнике, и
    подставлять для внутренней посылки категорию вложения международной
    было бы выдумкой, поэтому поле передаётся только когда вызывающий сам
    назвал его в ``extras``. Это первое, что проверяется на стенде.
    """
    place = require_single_place(req)
    payload: dict[str, Any] = {
        "mail-type": product.mail_type,
        "mail-category": product.category_for(insurance=req.insurance),
        "transport-type": product.transport_type,
        "mass": mass_grams(place.weight_kg),
        "dimension": dimension_block(place),
        "index-from": _index(req.sender.postal_code, what="отправителя"),
        "index-to": _index(req.recipient.postal_code, what="получателя"),
        "inventory": False,
        "with-order-of-notice": False,
        "with-simple-notice": False,
    }
    if req.insurance and product.insured_category:
        # Объявленная ценность передаётся в минорных единицах — см. строку
        # документации ``mapping``: единица у входного поля не названа.
        payload["declared-value"] = req.declared_value.amount_minor
    entries_type = req.extras.get("entries_type")
    if isinstance(entries_type, str) and entries_type.strip():
        payload["entries-type"] = entries_type.strip()
    return payload


def _breakdown(body: dict[str, Any]) -> dict[str, Any]:
    """Расшифровка цены по составляющим, каждая с НДС."""
    result: dict[str, Any] = {}
    for field, label in _RATE_NAMES.items():
        amount = money_from_rate(body.get(field))
        if amount is not None:
            result[label] = amount
    return result


def _transit_days(body: dict[str, Any]) -> tuple[int, int]:
    """Вилка срока в днях. ``(0, 0)`` — срока в ответе нет.

    Блок помечен опциональным целиком, и ``min-days`` внутри него — тоже.
    Отсутствующий минимум читается как максимум: вилка «от нуля» обещала бы
    доставку сегодня и выиграла бы любое сравнение по скорости.
    """
    block = body.get("delivery-time")
    if not isinstance(block, dict):
        return 0, 0
    raw_max = block.get("max-days")
    raw_min = block.get("min-days")
    max_days = int(raw_max) if isinstance(raw_max, int) and not isinstance(raw_max, bool) else 0
    min_days = int(raw_min) if isinstance(raw_min, int) and not isinstance(raw_min, bool) else 0
    if not max_days:
        return (min_days, min_days) if min_days else (0, 0)
    if not min_days or min_days > max_days:
        return max_days, max_days
    return min_days, max_days


def parse_tariff(
    body: dict[str, Any], product: PochtaProduct, *, price_source: PriceSource
) -> Quote | None:
    """Ответ расчёта → одно предложение. ``None`` — цены в ответе нет.

    Ответ без ``total-rate`` предложением не становится: ноль вывел бы Почту
    первой строкой как самую дешёвую, а на деле она просто не посчитала.
    """
    price = total_price(body)
    if price is None:
        log.info("pochta.tariff_without_price", product=product.code)
        return None

    min_days, max_days = _transit_days(body)
    promised = utcnow().date() + timedelta(days=max_days) if max_days else None
    return Quote(
        service_code=product.code,
        tariff_code=product.code,
        service_name=product.name,
        price=price,
        transit_days_min=min_days,
        transit_days_max=max_days,
        promised_delivery_date=promised,
        price_source=price_source,
        raw=body,
        price_breakdown=_breakdown(body),
    )
