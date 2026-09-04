"""Единицы, деньги и тарифы ПЭК.

Источник — официальная документация перевозчика
(`docs/integrations/sources/pecom/`, ADR-0020). Машинной спецификации у ПЭК
нет, поэтому каждое поле здесь сверено с текстом справки и с официальными
примерами запросов, а не с генератором.

Единицы, дословно из `help_calculator.html` и из официальных примеров
`examples/CalculatePrice/`:

* `weight` — «Вес, кг»;
* `length` / `width` / `height` — «Длина/Ширина/Высота груза, м»;
* `volume` — «Объем груза, м3»;
* `maxSize` — «Максимальный габарит, м»;
* `costTotal` — «Общая стоимость услуг по продукту/тарифу, **руб.**».

Валюта приходит числовым кодом ISO 4217 (`"643"`), а не буквенным.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aerogram.carriers.base import Place
from aerogram.shared.money import Money

__all__ = [
    "CURRENCY_BY_ISO_NUMERIC",
    "DEFAULT_TARIFF_TYPES",
    "PECOM_CURRENCY_RUB",
    "TARIFF_NAMES",
    "as_number",
    "cargos_block",
    "currency_from_code",
    "money_from_response",
    "to_metres",
    "volume_m3",
]

#: Код рубля в терминах ПЭК: числовой ISO 4217, строкой.
PECOM_CURRENCY_RUB: Final = "643"

#: Числовые коды ISO 4217 → буквенные. ПЭК отдаёт числовой, наш ``Money``
#: работает с буквенным. Список намеренно короткий: перевозчик возит
#: по России и ближнему зарубежью, а угадывать валюту нельзя — сумма
#: без верной валюты хуже отсутствующей.
CURRENCY_BY_ISO_NUMERIC: Final[dict[str, str]] = {
    "643": "RUB",
    "398": "KZT",
    "933": "BYN",
    "417": "KGS",
    "840": "USD",
    "978": "EUR",
}

#: Продукты ПЭК, доступные в API (`/typesOfDelivery/all/`).
#: Тариф 5 «ПЭК:Express Авто» в расчёте не участвует: документация прямо
#: предупреждает, что метод не умеет его считать.
TARIFF_NAMES: Final[dict[int, str]] = {
    1: "ПЭК:Express Авиа",
    3: "ПЭК:LTL Авто",
    12: "ПЭК:EasyWay Авто",
}

#: Что считаем, если вызывающий не указал иного. LTL Авто — основной продукт
#: сборных перевозок ПЭК и единственный, не требующий отдельного договора.
DEFAULT_TARIFF_TYPES: Final[tuple[int, ...]] = (3,)

_METRE_PLACES: Final = Decimal("0.001")
_KG_PLACES: Final = Decimal("0.001")
_M3_PLACES: Final = Decimal("0.000001")
_RUB_PLACES: Final = Decimal("0.01")


def as_number(value: Decimal, quantum: Decimal) -> float:
    """Число для тела запроса: округлить явно и отдать ``float``.

    ``float`` допустим только для габаритов, весов и объявленной стоимости
    груза — полей, которые ПЭК объявляет числами и строкой не примет.
    Суммы из ответа этим путём не ходят: их разбирает ``money_from_response``
    сразу в минорные единицы (CLAUDE.md §6).
    """
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


def to_metres(centimetres: int) -> Decimal:
    """Сантиметры в метры. Точно, без ``float``."""
    return Decimal(centimetres) / Decimal(100)


def volume_m3(place: Place) -> Decimal:
    """Объём одного места в кубометрах."""
    product = Decimal(place.length_cm) * Decimal(place.width_cm) * Decimal(place.height_cm)
    return product / Decimal(1_000_000)


def currency_from_code(code: object) -> str | None:
    """Буквенный код валюты по числовому коду ПЭК.

    ``None`` для неизвестного кода. Подставлять рубль по умолчанию нельзя:
    предложение в тенге, посчитанное как рублёвое, выиграет любое сравнение.
    """
    if code is None:
        return None
    return CURRENCY_BY_ISO_NUMERIC.get(str(code).strip())


def money_from_response(value: object, currency: str) -> Money | None:
    """Сумма из ответа ПЭК.

    Перевозчик отдаёт суммы числами (`"costTotal": 5319`, `"cost": 446.6`),
    но документация в одном месте помечает ту же величину как ``[String]``,
    поэтому принимается и строка.

    ``None`` — суммы нет. Это не ноль: тариф с ошибкой расчёта приходит
    без стоимости, и ноль вывел бы его первой строкой как самый дешёвый.

    ``float`` в деньгах запрещён, поэтому число переводится через своё
    текстовое представление: ``repr`` — кратчайшая строка, читающаяся
    обратно в то же число, то есть десятичная запись восстанавливается точно.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        if not text:
            return None
    elif isinstance(value, int | float):
        text = str(value)
    else:
        return None
    try:
        return Money.from_major(Decimal(text), currency)
    except (ArithmeticError, ValueError):
        return None


def cargos_block(places: tuple[Place, ...]) -> list[dict[str, object]]:
    """Массив ``cargos`` запроса расчёта.

    У ПЭК каждое грузовое место — отдельный элемент массива, со своими
    габаритами и весом, а не максимум по местам, как у Деловых Линий.
    Так и в официальном примере «расчёт нескольких грузомест».

    Объём передаётся вместе с габаритами: перевозчик считает платный вес
    сам, делителя в контракте нет (FR-1.2).
    """
    if not places:
        raise ValueError("расчёт без грузовых мест невозможен")
    return [
        {
            "length": as_number(to_metres(place.length_cm), _METRE_PLACES),
            "width": as_number(to_metres(place.width_cm), _METRE_PLACES),
            "height": as_number(to_metres(place.height_cm), _METRE_PLACES),
            "weight": as_number(place.weight_kg, _KG_PLACES),
            "volume": as_number(volume_m3(place), _M3_PLACES),
        }
        for place in places
    ]


def declared_value_rub(value: Money) -> float:
    """Объявленная стоимость для ``isInsurancePrice``: «сумма, руб. [Number]»."""
    return as_number(value.to_major(), _RUB_PLACES)
