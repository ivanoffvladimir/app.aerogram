"""Единицы, деньги и даты Деловых Линий.

Всё, что переводит наши величины в величины перевозчика и обратно, собрано
здесь: ошибка в единицах не даёт исключения, она даёт цену, отличающуюся
в сто раз. Источник — официальная OpenAPI 3.0.3 перевозчика,
``docs/integrations/sources/dellin/schema.yaml``.

Единицы у Деловых Линий, дословно из спеки:

* ``cargo.weight`` — «Вес самого тяжелого грузового места, кг»;
* ``cargo.totalWeight`` — «Общий вес груза, кг»;
* ``cargo.length/width/height`` — «Длина/Ширина/Высота самого … грузового
  места, м» (метры, не сантиметры);
* ``cargo.totalVolume`` — «Общий объём груза, куб. м»;
* ``cargo.insurance.statedValue`` — «Объявленная стоимость груза, руб.».

Наши ``Place`` хранят сантиметры целыми и вес в килограммах ``Decimal``,
поэтому перевод — деление на 100 и на 1 000 000, и делается он в ``Decimal``,
а не во ``float``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aerogram.carriers.base import Place
from aerogram.shared.money import Money

__all__ = [
    "CM_IN_M",
    "DEFAULT_DELIVERY_TYPE",
    "DELIVERY_TYPES",
    "RUB",
    "as_number",
    "cargo_block",
    "money_from_response",
    "parse_carrier_date",
    "to_metres",
    "total_volume_m3",
]

#: Валюта Деловых Линий. В спеке её нет ни в одном поле цены: суммы приходят
#: голым числом, а «руб.» стоит только в описании ``statedValue``. Значит
#: валюту проставляем мы, и подставить сюда что-то другое нельзя, пока
#: перевозчик не начнёт её возвращать.
RUB: Final = "RUB"

CM_IN_M: Final = Decimal(100)

#: ``delivery.deliveryType.type`` — виды межтерминальной перевозки.
DELIVERY_TYPES: Final[tuple[str, ...]] = ("auto", "express", "small", "letter", "avia")

#: Автодоставка — основной вид перевозки Деловых Линий. Запрос без явного
#: указания считается по ней.
DEFAULT_DELIVERY_TYPE: Final = "auto"


#: Знаков после запятой при передаче габаритов и весов перевозчику.
#: Метр с точностью до миллиметра и килограмм до грамма — больше, чем нужно
#: для тарификации, и меньше, чем шум от лишних разрядов.
_METRE_PLACES: Final = Decimal("0.001")
_KG_PLACES: Final = Decimal("0.001")
_M3_PLACES: Final = Decimal("0.000001")


def as_number(value: Decimal, quantum: Decimal) -> float:
    """Число для тела запроса: округлить явно и отдать ``float``.

    ``float`` здесь допустим и появляется **только** для габаритов и весов:
    поля ``cargo`` объявлены в спеке как ``number``, строку перевозчик там
    не примет, а стандартный сериализатор JSON ``Decimal`` не умеет.

    Деньги этим путём не ходят никогда (CLAUDE.md §6): объявленная стоимость
    уходит строкой в ``insurance.statedValue``, а суммы из ответа разбирает
    ``money_from_response`` в минорные единицы.

    Округление явное и до вызова ``float``, чтобы результат не зависел
    от двоичного представления.
    """
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


def to_metres(centimetres: int) -> Decimal:
    """Сантиметры в метры. Точно, без ``float``."""
    return Decimal(centimetres) / CM_IN_M


def total_volume_m3(places: tuple[Place, ...]) -> Decimal:
    """Суммарный объём мест в кубометрах.

    Считается из сантиметров одним делением на миллион, а не тремя делениями
    на сто: так меньше промежуточных округлений.
    """
    total = sum(
        (Decimal(p.length_cm) * Decimal(p.width_cm) * Decimal(p.height_cm) for p in places),
        start=Decimal(0),
    )
    return total / Decimal(1_000_000)


def money_from_response(value: object, currency: str = RUB) -> Money | None:
    """Сумма из ответа Деловых Линий.

    Принимает и число, и строку, потому что перевозчик отдаёт **и то и другое**:
    спека объявляет ``price`` как ``type: string``, а её собственный пример
    ответа содержит ``"price": 475`` и ``"price": 320.0`` числами. Полагаться
    на объявленный тип здесь нельзя.

    ``None`` возвращается для отсутствующей цены. Это не ноль: у договорной
    цены (``contractPrice: true``) поле приходит пустым, и подставить ноль
    значило бы вывести такое предложение первой строкой как самое дешёвое.

    ``float`` в деньгах запрещён (CLAUDE.md §6), поэтому число переводится
    через его текстовое представление: ``repr`` числа с плавающей точкой —
    кратчайшая строка, которая читается обратно в то же число, то есть
    десятичная запись перевозчика восстанавливается точно.
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


def parse_carrier_date(value: object) -> date | None:
    """Дата из ответа перевозчика.

    Деловые Линии отдают в ``orderDates`` и ``"2019-11-26"``, и
    ``"2019-11-28 00:00:00"`` — в соседних полях одного объекта. Оба варианта
    разбираются, всё остальное считается отсутствующей датой.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def cargo_block(
    places: tuple[Place, ...], declared_value: Money, *, insure: bool
) -> dict[str, object]:
    """Блок ``cargo`` запроса расчёта.

    Габариты у Деловых Линий — не сумма и не список, а **максимум по местам**:
    «длина самого длинного», «вес самого тяжёлого». Общими передаются только
    вес и объём. Объёмный вес перевозчик считает сам, поэтому делитель
    на нашей стороне для него не применяется (FR-1.2).
    """
    if not places:
        raise ValueError("расчёт без грузовых мест невозможен")

    cargo: dict[str, object] = {
        "quantity": len(places),
        "length": as_number(to_metres(max(p.length_cm for p in places)), _METRE_PLACES),
        "width": as_number(to_metres(max(p.width_cm for p in places)), _METRE_PLACES),
        "height": as_number(to_metres(max(p.height_cm for p in places)), _METRE_PLACES),
        "weight": as_number(max(p.weight_kg for p in places), _KG_PLACES),
        "totalWeight": as_number(sum((p.weight_kg for p in places), start=Decimal(0)), _KG_PLACES),
        "totalVolume": as_number(total_volume_m3(places), _M3_PLACES),
    }
    if insure:
        # statedValue объявлена строкой и в спеке, и в примерах — здесь
        # перевозчик последователен, поэтому строку и передаём.
        cargo["insurance"] = {
            "statedValue": str(declared_value.to_major()),
            "term": False,
        }
    return cargo
