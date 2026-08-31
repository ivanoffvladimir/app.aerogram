"""Деньги — целое число минорных единиц и код валюты. Вес — Decimal.

Денежная величина никогда не бывает `float` и никогда не бывает без валюты
(ADR-0011). `Decimal` остаётся для неденежных дробных величин: вес, доли,
проценты.

Почему минорные единицы, а не `Decimal`: деньги ходят через четыре границы —
адаптер перевозчика, наша БД, наш API и фронт, — и `Decimal` не переживает JSON.
Целое `2410000` одинаково читается везде, строка `"24100.00"` рано или поздно
попадёт в `parseFloat`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = [
    "DEFAULT_MINOR_UNIT_EXPONENT",
    "DEFAULT_VOLUMETRIC_DIVISOR",
    "WEIGHT_QUANT",
    "CurrencyMismatchError",
    "Money",
    "chargeable_weight",
    "format_ru",
    "minor_unit_exponent",
    "mm_to_cm",
    "round_weight",
    "total",
    "volumetric_weight",
]

WEIGHT_QUANT = Decimal("0.001")

#: Делитель объёмного веса ПО УМОЛЧАНИЮ. У каждого перевозчика он свой
#: и хранится в справочнике (``carriers.volumetric_divisor``): ориентир IATA —
#: 6000, но часть перевозчиков применяет 5000, и коэффициент может зависеть
#: от типа груза и направления.
#:
#: Значение по умолчанию выбрано в сторону осторожности: меньший делитель даёт
#: больший объёмный вес, то есть более высокую котировку. Занижение стоит денег
#: напрямую — счёт от перевозчика придёт по ЕГО коэффициенту, а не по нашему.
DEFAULT_VOLUMETRIC_DIVISOR = 5000

#: Сколько знаков в минорной единице у большинства валют.
DEFAULT_MINOR_UNIT_EXPONENT: Final = 2

#: Валюты, у которых число знаков отличается от двух (ISO 4217).
#: Список неполный намеренно: сюда добавляется валюта, с которой мы реально
#: работаем, а не весь справочник, который потом некому проверять.
_MINOR_UNIT_EXPONENTS: Final[dict[str, int]] = {
    "JPY": 0,
    "KRW": 0,
    "CLP": 0,
    "VND": 0,
    "ISK": 0,
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
}


class CurrencyMismatchError(ValueError):
    """Попытка сложить или сравнить суммы в разных валютах.

    Это ошибка нашего кода, а не запроса клиента, поэтому не наследуется
    от ``AerogramError``: превращать её в 400 значило бы обвинить клиента
    в нашей ошибке.
    """

    def __init__(self, left: str, right: str) -> None:
        super().__init__(f"нельзя работать с {left} и {right} как с одной валютой")
        self.left = left
        self.right = right


def minor_unit_exponent(currency: str) -> int:
    """Число знаков после запятой у валюты."""
    return _MINOR_UNIT_EXPONENTS.get(currency.upper(), DEFAULT_MINOR_UNIT_EXPONENT)


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """Сумма в минорных единицах и валюта ISO 4217.

    Неизменяема: сумма из снимка решения не должна меняться из-за того,
    что кто-то держит на неё ссылку.
    """

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise TypeError("сумма задаётся целым числом минорных единиц")
        code = self.currency.upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(
                f"код валюты должен быть тремя буквами ISO 4217, получено {self.currency!r}"
            )
        object.__setattr__(self, "currency", code)

    # --- конструкторы ---

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    @classmethod
    def from_major(cls, value: Decimal | int | str, currency: str) -> Money:
        """Из основной единицы: ``Money.from_major("241.00", "RUB")`` → 24100 копеек.

        Нужен на границе с перевозчиками, которые отдают суммы в рублях.
        Округление явное: ROUND_HALF_UP до минорной единицы.
        """
        exponent = minor_unit_exponent(currency)
        scaled = Decimal(value) * (10**exponent)
        return cls(int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)), currency)

    # --- представление ---

    def to_major(self) -> Decimal:
        """В основную единицу — только для отображения и выгрузок, не для арифметики."""
        exponent = minor_unit_exponent(self.currency)
        return Decimal(self.amount_minor).scaleb(-exponent)

    def __str__(self) -> str:
        """Отладочное представление. Человеку показывается ``format_ru``."""
        return f"{self.to_major()} {self.currency}"

    # --- арифметика ---

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    def __mul__(self, factor: int) -> Money:
        """Умножение на целое — например, цена места на количество мест."""
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError("деньги умножаются только на целое; для долей есть percentage")
        return Money(self.amount_minor * factor, self.currency)

    __rmul__ = __mul__

    def percentage(self, rate_percent: Decimal) -> Money:
        """Процент от суммы, ROUND_HALF_UP до минорной единицы.

        ``rate_percent`` — именно проценты: ``Decimal("0.18")`` это 0.18 %,
        как в ``cost_components[].rate_percent`` контракта API.
        """
        if rate_percent < 0:
            raise ValueError("ставка не может быть отрицательной")
        exact = Decimal(self.amount_minor) * rate_percent / Decimal(100)
        return Money(int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)), self.currency)

    # --- сравнение ---

    def __lt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.amount_minor >= other.amount_minor


#: Символы валют, с которыми мы работаем. Для остальных показывается код:
#: выдуманный символ хуже честного «CNY».
_CURRENCY_SYMBOLS: Final[dict[str, str]] = {
    "RUB": "\u20bd",
    "USD": "$",
    "EUR": "\u20ac",
    "CNY": "\u00a5",
}

#: Неразрывный пробел: сумма не должна разрываться переносом строки.
_NBSP: Final = "\u00a0"


def format_ru(money: Money) -> str:
    """Сумма для человека: русский формат, как на фронте.

    Интерфейс, письма и тексты ошибок у нас русские (CLAUDE.md §6), а
    ``str(Money)`` даёт отладочное «870.00 RUB». Формат совпадает с тем, что
    выдаёт ``Intl.NumberFormat('ru-RU')`` на фронте: запятая как разделитель
    дробной части, неразрывный пробел между разрядами и перед символом.
    """
    exponent = minor_unit_exponent(money.currency)
    symbol = _CURRENCY_SYMBOLS.get(money.currency, money.currency)
    sign = "-" if money.amount_minor < 0 else ""
    digits = str(abs(money.amount_minor)).rjust(exponent + 1, "0")
    whole, fraction = (digits[:-exponent], digits[-exponent:]) if exponent else (digits, "")

    groups = [whole[max(i - 3, 0) : i] for i in range(len(whole), 0, -3)][::-1]
    body = _NBSP.join(groups)
    if fraction:
        body = f"{body},{fraction}"
    return f"{sign}{body}{_NBSP}{symbol}"


def mm_to_cm(value: int | None) -> int:
    """Миллиметры контракта → сантиметры адаптеров, вверх до целого.

    Округление вниз занизило бы объёмный вес и, значит, цену: 305 мм это 31 см
    для тарифа, а не 30. Отсутствующий габарит даёт 1 см, а не ноль: нулевой
    габарит запрещён проверкой объёмного веса.

    Живёт рядом с весом, потому что нужен и расчёту, и созданию отправления:
    посчитать по одним габаритам, а отправить по другим — та же ошибка, что
    посчитать в одной валюте, а выставить в другой.
    """
    if value is None:
        return 1
    return max(1, -(-value // 10))


def total(amounts: list[Money], currency: str) -> Money:
    """Сумма списка. Валюта передаётся явно: у пустого списка её взять неоткуда."""
    result = Money.zero(currency)
    for amount in amounts:
        result = result + amount
    return result


def round_weight(value: Decimal) -> Decimal:
    """Округлить вес до грамма."""
    return value.quantize(WEIGHT_QUANT, rounding=ROUND_HALF_UP)


def volumetric_weight(
    length_cm: int,
    width_cm: int,
    height_cm: int,
    divisor: int = DEFAULT_VOLUMETRIC_DIVISOR,
) -> Decimal:
    """Объёмный вес в килограммах: Д × Ш × В (см) / делитель."""
    if min(length_cm, width_cm, height_cm) <= 0:
        raise ValueError("габариты должны быть положительными")
    if divisor <= 0:
        raise ValueError("делитель объёмного веса должен быть положительным")
    volume = Decimal(length_cm) * Decimal(width_cm) * Decimal(height_cm)
    return round_weight(volume / Decimal(divisor))


def chargeable_weight(
    actual_kg: Decimal,
    length_cm: int,
    width_cm: int,
    height_cm: int,
    divisor: int = DEFAULT_VOLUMETRIC_DIVISOR,
) -> Decimal:
    """Расчётный вес места: максимум из фактического и объёмного.

    Применяется только если перевозчик не считает объёмный вес сам — иначе
    получится двойной учёт. Решение принимается в адаптере, по capabilities.
    """
    if actual_kg <= 0:
        raise ValueError("фактический вес должен быть положительным")
    return max(round_weight(actual_kg), volumetric_weight(length_cm, width_cm, height_cm, divisor))
