"""Деньги, вес и объёмный вес (FR-1.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aerogram.shared.money import (
    chargeable_weight,
    round_money,
    round_weight,
    volumetric_weight,
)


class TestRoundMoney:
    def test_rounds_half_up_as_accounting_expects(self) -> None:
        # Банковское округление здесь неприемлемо: 2.345 должно давать 2.35,
        # иначе суммы расходятся со счётом перевозчика на копейки.
        assert round_money(Decimal("2.345")) == Decimal("2.35")
        assert round_money(Decimal("2.344")) == Decimal("2.34")

    def test_keeps_two_decimals(self) -> None:
        assert round_money(Decimal("100")) == Decimal("100.00")


class TestVolumetricWeight:
    def test_default_divisor_5000(self) -> None:
        # 40 × 30 × 25 = 30000 см³ / 5000 = 6 кг
        assert volumetric_weight(40, 30, 25) == Decimal("6.000")

    def test_carrier_specific_divisor(self) -> None:
        assert volumetric_weight(40, 30, 25, divisor=6000) == Decimal("5.000")

    @pytest.mark.parametrize(
        ("length", "width", "height"),
        [(0, 10, 10), (10, 0, 10), (10, 10, 0), (-1, 10, 10)],
    )
    def test_rejects_non_positive_dimensions(self, length: int, width: int, height: int) -> None:
        with pytest.raises(ValueError, match="габариты"):
            volumetric_weight(length, width, height)

    def test_rejects_non_positive_divisor(self) -> None:
        with pytest.raises(ValueError, match="делитель"):
            volumetric_weight(40, 30, 25, divisor=0)


class TestChargeableWeight:
    def test_takes_volumetric_when_it_is_larger(self) -> None:
        # Лёгкая объёмная коробка: платим за объём, а не за вес.
        assert chargeable_weight(Decimal("2"), 40, 30, 25) == Decimal("6.000")

    def test_takes_actual_when_it_is_larger(self) -> None:
        # Плотный тяжёлый груз: платим за фактический вес.
        assert chargeable_weight(Decimal("12"), 40, 30, 25) == Decimal("12.000")

    def test_rejects_non_positive_actual_weight(self) -> None:
        with pytest.raises(ValueError, match="фактический вес"):
            chargeable_weight(Decimal("0"), 40, 30, 25)


def test_round_weight_keeps_grams() -> None:
    assert round_weight(Decimal("1.2345")) == Decimal("1.235")
