"""Оформление у Почты России: заказ, поиск по нашему номеру, печатная форма.

Написано по официальной документации API «Отправка» (планка ADR-0020),
страницы `orders-creating_order_v2`, `orders-search_order`
и `documents-create_f7_f22`.

**Заказ создаётся версией 2.0, и только ею.** `PUT /2.0/user/backlog`
возвращает `orders[].barcode` — «ШПИ отправления», то есть трек-номер есть
сразу. Версия 1.0 отдаёт только внутренние идентификаторы, и справка сама
зовёт её «Создание заказа без ШПИ»: отправление, у которого нечего показать
клиенту до формирования партии, — не отправление, а обещание.

**Сверка «призраков» работает без единой выдумки.** У Почты есть поле
`order-num` — «Внешний идентификатор заказа, который формируется
отправителем», — туда уходит наш номер, а `GET /1.0/backlog/search?query=`
ищет «по назначенному магазином идентификатору». Если ответ на создание
не дошёл, платформа спрашивает у перевозчика по своему же номеру (FR-2.5).

**Адрес Почте нужен разобранным, а нашему контракту хватает строки.**
`Party` несёт город, индекс и одну строку адреса, а `/2.0/user/backlog`
требует `region-to`, `street-to` и `house-to` порознь. Разбирать строку
регулярным выражением здесь запрещено здравым смыслом: ошибка разбора
отправляет груз к другому дому, и молча. Поэтому части адреса берутся
из `extras`, а без них оформление отклоняется до вызова перевозчика.

**Индекс получателя у Почты то строка, то число.** В расчёте `index-to`
объявлен строкой, в создании заказа — целым. Это расхождение самого
контракта перевозчика, а не наша вольность, и оно здесь названо, чтобы
следующий читатель не счёл его опечаткой.

**Домен `extras` пока не заполняет ничем.** `ShipmentRequest` собирается
в `shipments.service` без них, и в контракте API поля для них нет вовсе.
Значит оформление Почтой из кабинета сегодня отказывает — с внятным
текстом, а не молча, — и заработает, когда части адреса и ФИО получателя
будет чем донести до адаптера. Это правка `carriers/base.py`, то есть
решение человека и ADR (CLAUDE.md §7, пункт 3), а не вольность адаптера:
остальные перевозчики принимают адрес одной строкой и деградируют мягко,
Почта — не может, у неё эти поля обязательные.
"""

from __future__ import annotations

from typing import Any, Final

from aerogram.carriers.base import Party, ShipmentRequest, ShipmentResult
from aerogram.carriers.pochta.mapping import PochtaProduct, dimension_block, mass_grams
from aerogram.shared.errors import CarrierValidationError
from aerogram.shared.logging import get_logger

__all__ = [
    "BACKLOG_PATH",
    "POSTOFFICE_SETTING",
    "RUSSIA_COUNTRY_CODE",
    "SEARCH_PATH",
    "create_payload",
    "form_path",
    "parse_created",
    "parse_found",
]

log = get_logger(__name__)

POCHTA_CODE: Final = "pochta"

#: Создание заказа. Версия 2.0 намеренно: см. строку документации модуля.
BACKLOG_PATH: Final = "/2.0/user/backlog"

#: Поиск заказа по идентификатору, назначенному отправителем.
SEARCH_PATH: Final = "/1.0/backlog/search"

#: Код России в справочнике стран Почты (`dictionary-countries`), он же
#: числовой ISO 3166. `mail-direct` — «Код страны назначения»; для
#: внутренних отправлений это Россия.
RUSSIA_COUNTRY_CODE: Final = 643

#: Тип адреса по умолчанию: обычный адрес, а не почтомат и не «до
#: востребования» (`enums-base-address-type`).
DEFAULT_ADDRESS_TYPE: Final = "DEFAULT"

#: Ключ настройки учётной записи с индексом отделения приёма
#: («postoffice-code — Индекс места приема»). Это свойство договора,
#: а не отправления: сдаёт груз тенант, и всегда в своё отделение.
POSTOFFICE_SETTING: Final = "postoffice_code"


def form_path(order_id: str) -> str:
    """Путь печатной формы Ф7п для заказа."""
    return f"/1.0/forms/{order_id}/f7pdf"


def _required(value: object, *, what: str, key: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CarrierValidationError(
            f"Для оформления у Почты России нужен {what}: передайте «{key}» в extras запроса",
            field=key,
            carrier_code=POCHTA_CODE,
        )
    return text


def _index(value: str | None, *, what: str) -> int:
    """Почтовый индекс числом — так его требует создание заказа.

    В расчёте то же поле объявлено строкой; расхождение принадлежит
    контракту перевозчика (см. строку документации модуля).
    """
    text = (value or "").strip()
    if not text.isdigit():
        raise CarrierValidationError(
            f"Для оформления у Почты России нужен почтовый индекс {what}",
            carrier_code=POCHTA_CODE,
        )
    return int(text)


def _name_parts(party: Party, extras: dict[str, object]) -> tuple[str, str]:
    """Фамилия и имя получателя.

    Почта требует их отдельными полями даже тогда, когда получатель —
    организация, а у нас есть только «контактное лицо» одной строкой.
    Разбор здесь самый простой: первое слово — фамилия, второе — имя,
    как в русских формах. Догадка названа догадкой, и её перекрывает
    ``extras``: там, где известны настоящие части имени, гадать не нужно.
    """
    surname = str(extras.get("surname") or "").strip()
    given = str(extras.get("given_name") or "").strip()
    if surname and given:
        return surname, given

    words = (party.contact_person or "").split()
    if len(words) < 2:
        raise CarrierValidationError(
            "Для оформления у Почты России нужны фамилия и имя получателя: "
            "укажите контактное лицо или передайте «surname» и «given_name» в extras",
            field="contact_person",
            carrier_code=POCHTA_CODE,
        )
    return words[0], words[1]


def create_payload(
    req: ShipmentRequest, product: PochtaProduct, *, sender_index: str | None
) -> list[dict[str, Any]]:
    """Тело ``PUT /2.0/user/backlog``: массив из одного заказа.

    Массив, потому что метод принимает пачку. Мы кладём один заказ:
    одно РПО — одно отправление, и групповая отправка — это массовые
    отправления нашего собственного модуля, а не пачка внутри одного вызова.
    """
    if len(req.places) != 1:
        raise CarrierValidationError(
            "Почта России оформляет одно грузовое место в отправлении, "
            f"а в запросе их {len(req.places)}",
            carrier_code=POCHTA_CODE,
        )
    place = req.places[0]
    extras = req.extras
    surname, given_name = _name_parts(req.recipient, extras)

    order: dict[str, Any] = {
        # Наш номер уходит перевозчику: по нему потом идёт сверка «призраков».
        "order-num": req.number,
        "mail-type": product.mail_type,
        "mail-category": product.category_for(insurance=req.insurance),
        # Справка зовёт это поле опциональным и «для международных
        # отправлений», хотя в расчёте оно обязательное и именно им
        # различаются наши продукты. Отправляем то же значение, которым
        # считали цену: лишнее поле перевозчик отклонит вслух, а
        # пропущенное молча заменит вид пересылки — и заказ уедет
        # по цене, которой мы не показывали.
        "transport-type": product.transport_type,
        "mail-direct": RUSSIA_COUNTRY_CODE,
        "mass": mass_grams(place.weight_kg),
        "dimension": dimension_block(place),
        "address-type-to": DEFAULT_ADDRESS_TYPE,
        "index-to": _index(req.recipient.postal_code, what="получателя"),
        "region-to": _required(extras.get("region"), what="регион получателя", key="region"),
        "place-to": req.recipient.city_name,
        "street-to": _required(extras.get("street"), what="улица получателя", key="street"),
        "house-to": _required(extras.get("house"), what="дом получателя", key="house"),
        "recipient-name": req.recipient.name or req.recipient.contact_person or "",
        "surname": surname,
        "given-name": given_name,
        "postoffice-code": _required(
            extras.get(POSTOFFICE_SETTING) or sender_index or req.sender.postal_code,
            what="индекс отделения приёма",
            key=POSTOFFICE_SETTING,
        ),
        "fragile": bool(extras.get("fragile", False)),
        # Полный тариф без скидочного: второй нам не с чем сверять, а выбор
        # между ними стал бы решением о цене, которого никто не принимал.
        "tariff-count": 1,
        "inventory": False,
        "with-order-of-notice": False,
        "with-simple-notice": False,
    }
    if req.recipient.phone:
        # Телефон Почта принимает числом; всё, кроме цифр, отбрасывается.
        digits = "".join(ch for ch in req.recipient.phone if ch.isdigit())
        if digits:
            order["tel-address"] = int(digits[-10:])
    if flat := str(extras.get("flat") or "").strip():
        order["room-to"] = flat
    if req.insurance and product.insured_category:
        order["insr-value"] = req.declared_value.amount_minor
    return [order]


def parse_created(body: dict[str, Any], *, number: str) -> ShipmentResult:
    """Ответ создания → результат.

    ``external_id`` — внутренний идентификатор заказа: именно его принимают
    печатные формы и удаление. ``tracking_number`` — ШПИ, он же то, что
    клиент увидит на трекинге.
    """
    orders = body.get("orders")
    if not isinstance(orders, list) or not orders:
        message = _first_error(body) or "Почта России не вернула созданный заказ"
        raise CarrierValidationError(message, carrier_code=POCHTA_CODE)

    order = orders[0]
    if not isinstance(order, dict):
        raise CarrierValidationError(
            "Почта России вернула заказ неожиданного вида", carrier_code=POCHTA_CODE
        )
    result_id = order.get("result-id")
    barcode = str(order.get("barcode") or "").strip() or None
    if result_id is None:
        raise CarrierValidationError(
            "Почта России не вернула идентификатор заказа", carrier_code=POCHTA_CODE
        )
    if barcode is None:
        # Заказ создан, ШПИ нет: так отвечает версия 1.0, и попасть сюда
        # можно только сменой пути. Молчать нельзя — отправление без
        # трек-номера выглядит созданным, а показать клиенту нечего.
        log.warning("pochta.order_without_barcode", number=number)
    return ShipmentResult(
        external_id=str(result_id),
        tracking_number=barcode,
        promised_delivery_date=None,
        price_actual=None,
        is_pending=barcode is None,
        raw=order,
    )


def parse_found(body: object, *, number: str) -> ShipmentResult | None:
    """Ответ поиска → результат или ``None``, если заказа нет.

    Поиск отдаёт массив: перевозчик ищет по подстроке, и совпадение
    сверяется по точному равенству нашему номеру. Иначе сверка «призраков»
    приняла бы за наш заказ чужой, у которого номер начинается так же.
    """
    if not isinstance(body, list):
        return None
    for item in body:
        if not isinstance(item, dict):
            continue
        if str(item.get("order-num") or "").strip() != number:
            continue
        order_id = item.get("id")
        if order_id is None:
            continue
        barcode = str(item.get("barcode") or "").strip() or None
        return ShipmentResult(
            external_id=str(order_id),
            tracking_number=barcode,
            promised_delivery_date=None,
            price_actual=None,
            is_pending=barcode is None,
            raw=item,
        )
    return None


def _first_error(body: dict[str, Any]) -> str | None:
    """Первая внятная ошибка из конверта создания.

    Конверт свой: ``errors[].error-codes[].{code, description, details}``.
    Общий разбор клиента его не узнаёт — и правильно делает: в успешном
    ответе создания массив `errors` может быть пустым, а сам ответ
    несёт заказы.

    ``UNDEFINED`` здесь **ошибка**, а не заглушка схемы: справочник
    `enums-errors` переводит его как «Неопределенная ошибка». Поэтому
    код показывается вместе с описанием, а один только код — сам по себе:
    отказ без текста всё равно остаётся отказом.
    """
    errors = body.get("errors")
    if not isinstance(errors, list):
        return None
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        codes = entry.get("error-codes")
        if not isinstance(codes, list):
            continue
        for code in codes:
            if not isinstance(code, dict):
                continue
            text = " ".join(
                str(code.get(key) or "").strip()
                for key in ("code", "description", "details")
                if str(code.get(key) or "").strip()
            )
            if text:
                return text
    return None
