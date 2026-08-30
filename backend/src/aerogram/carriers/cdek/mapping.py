"""Нормализация тарифов СДЭК.

Отдельный модуль без ввода-вывода: сопоставление режимов доставки и тарифов —
предметное знание, которое меняется независимо от кода запросов и обязано
проверяться без сети.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from aerogram.shared.enums import DeliveryMode

__all__ = [
    "CDEK_CURRENCY_RUB",
    "CDEK_TYPE_DELIVERY",
    "DELIVERY_MODES",
    "grams_from_kg",
    "modes_for_request",
]

#: Тип заказа: 1 — интернет-магазин, 2 — доставка.
#: Для режима own_contract (договор клиента, B2B-отправитель) — доставка.
CDEK_TYPE_DELIVERY: Final = 2

#: Код валюты СДЭК: 1 — российский рубль.
CDEK_CURRENCY_RUB: Final = 1

#: Режимы доставки СДЭК → наша модель.
#: 1 дверь-дверь, 2 дверь-склад, 3 склад-дверь, 4 склад-склад,
#: 6 дверь-постамат, 7 склад-постамат.
DELIVERY_MODES: Final[dict[int, DeliveryMode]] = {
    1: DeliveryMode.DOOR_DOOR,
    2: DeliveryMode.DOOR_TERMINAL,
    3: DeliveryMode.TERMINAL_DOOR,
    4: DeliveryMode.TERMINAL_TERMINAL,
    6: DeliveryMode.DOOR_TERMINAL,
    7: DeliveryMode.TERMINAL_TERMINAL,
}


def modes_for_request(*, pickup: bool, delivery_to_door: bool) -> frozenset[int]:
    """Коды режимов СДЭК, отвечающие запросу.

    Забор от адреса и доставка до адреса — независимые опции формы расчёта
    (FR-1.1), и вместе они однозначно задают пару «откуда — куда».
    Постаматы попадают в ту же группу, что и склады: для отправителя это
    один и тот же сценарий «получатель забирает сам».

    Возврат всех режимов подряд сделал бы выдачу нечитаемой: пользователь,
    попросивший доставку до двери, увидел бы вперемешку цены до пункта выдачи,
    которые всегда ниже, и выбрал бы не то.
    """
    if pickup and delivery_to_door:
        return frozenset({1})
    if pickup and not delivery_to_door:
        return frozenset({2, 6})
    if not pickup and delivery_to_door:
        return frozenset({3})
    return frozenset({4, 7})


def grams_from_kg(weight_kg: Decimal) -> int:
    """Перевести килограммы в граммы: калькулятор СДЭК принимает вес в граммах.

    Ошибка в единице измерения здесь не даёт исключения — она даёт цену,
    отличающуюся в тысячу раз, и обнаруживается счётом от перевозчика.
    Округление вверх: перевозчик тарифицирует по фактическому весу, и занизить
    его означает недобрать с клиента.
    """
    if weight_kg <= 0:
        raise ValueError("вес должен быть положительным")
    grams = weight_kg * 1000
    rounded = int(grams)
    return rounded if Decimal(rounded) == grams else rounded + 1
