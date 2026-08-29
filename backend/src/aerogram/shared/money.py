"""Деньги и вес. Только Decimal.

float для денег — ошибка ревью (CLAUDE.md §6): 0.1 + 0.2 != 0.3 всплывает в сверке
с перевозчиком через месяцы после релиза.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "DEFAULT_VOLUMETRIC_DIVISOR",
    "MONEY_QUANT",
    "WEIGHT_QUANT",
    "chargeable_weight",
    "round_money",
    "round_weight",
    "volumetric_weight",
]

MONEY_QUANT = Decimal("0.01")
WEIGHT_QUANT = Decimal("0.001")

#: Делитель объёмного веса по умолчанию (FR-1.2). Переопределяется на уровне перевозчика.
DEFAULT_VOLUMETRIC_DIVISOR = 5000


def round_money(value: Decimal) -> Decimal:
    """Округлить сумму до копеек, ROUND_HALF_UP — как считает бухгалтерия."""
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def round_weight(value: Decimal) -> Decimal:
    """Округлить вес до грамма."""
    return value.quantize(WEIGHT_QUANT, rounding=ROUND_HALF_UP)


def volumetric_weight(
    length_cm: int,
    width_cm: int,
    height_cm: int,
    divisor: int = DEFAULT_VOLUMETRIC_DIVISOR,
) -> Decimal:
    """Объёмный вес в килограммах: Д × Ш × В (см) / делитель (FR-1.2)."""
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
    """Расчётный вес места: максимум из фактического и объёмного (FR-1.2).

    Применяется только если перевозчик не считает объёмный вес сам — иначе
    получится двойной учёт. Решение принимается в адаптере, по capabilities.
    """
    if actual_kg <= 0:
        raise ValueError("фактический вес должен быть положительным")
    return max(round_weight(actual_kg), volumetric_weight(length_cm, width_cm, height_cm, divisor))
