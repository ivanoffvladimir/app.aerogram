"""Деньги, вес и объёмный вес (FR-1.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aerogram.shared.money import (
    CurrencyMismatchError,
    Money,
    chargeable_weight,
    format_ru,
    minor_unit_exponent,
    round_weight,
    total,
    volumetric_weight,
)


class TestMoneyConstruction:
    def test_currency_is_normalised_to_upper_case(self) -> None:
        assert Money(100, "rub").currency == "RUB"

    @pytest.mark.parametrize("bad", ["RU", "RUBL", "R7B", ""])
    def test_rejects_codes_that_are_not_iso_4217(self, bad: str) -> None:
        with pytest.raises(ValueError, match="ISO 4217"):
            Money(100, bad)

    def test_rejects_decimal_amount(self) -> None:
        # Дробная сумма минорных единиц означает, что кто-то передал рубли
        # вместо копеек. Молча округлить — потерять два порядка.
        with pytest.raises(TypeError):
            Money(Decimal("100.50"), "RUB")  # type: ignore[arg-type]

    def test_rejects_bool_as_amount(self) -> None:
        # bool — подкласс int, и True прошло бы как «1 копейка».
        with pytest.raises(TypeError):
            Money(True, "RUB")  # type: ignore[arg-type]


class TestMoneyMajorUnits:
    def test_from_major_scales_by_currency_exponent(self) -> None:
        assert Money.from_major("241.00", "RUB").amount_minor == 24100
        # У иены нет минорной единицы: 241 иена это 241, а не 24100.
        assert Money.from_major("241", "JPY").amount_minor == 241

    def test_from_major_rounds_half_up(self) -> None:
        assert Money.from_major(Decimal("2.345"), "RUB").amount_minor == 235
        assert Money.from_major(Decimal("2.344"), "RUB").amount_minor == 234

    def test_round_trip_through_major_units(self) -> None:
        assert Money(24100, "RUB").to_major() == Decimal("241.00")

    def test_minor_unit_exponent_defaults_to_two(self) -> None:
        assert minor_unit_exponent("RUB") == 2
        assert minor_unit_exponent("JPY") == 0
        assert minor_unit_exponent("XYZ") == 2


class TestMoneyArithmetic:
    def test_adds_and_subtracts_within_one_currency(self) -> None:
        assert Money(100, "RUB") + Money(50, "RUB") == Money(150, "RUB")
        assert Money(100, "RUB") - Money(50, "RUB") == Money(50, "RUB")

    def test_multiplies_by_whole_number_of_places(self) -> None:
        assert Money(24100, "RUB") * 3 == Money(72300, "RUB")
        assert 3 * Money(24100, "RUB") == Money(72300, "RUB")

    def test_refuses_multiplication_by_float(self) -> None:
        # Умножение на дробь — это доля, и она обязана иметь явное округление.
        with pytest.raises(TypeError, match="percentage"):
            Money(24100, "RUB") * 1.5  # type: ignore[operator]

    @pytest.mark.parametrize(
        "operation",
        [
            lambda a, b: a + b,
            lambda a, b: a - b,
            lambda a, b: a < b,
            lambda a, b: a >= b,
        ],
    )
    def test_never_mixes_currencies(self, operation: object) -> None:
        with pytest.raises(CurrencyMismatchError):
            operation(Money(100, "RUB"), Money(100, "USD"))  # type: ignore[operator]

    def test_equality_distinguishes_currency(self) -> None:
        assert Money(100, "RUB") != Money(100, "USD")

    def test_total_of_empty_list_needs_explicit_currency(self) -> None:
        assert total([], "RUB") == Money(0, "RUB")

    def test_total_sums_components(self) -> None:
        components = [Money(2140000, "RUB"), Money(270000, "RUB")]
        assert total(components, "RUB") == Money(2410000, "RUB")


class TestMoneyPercentage:
    def test_matches_the_reference_example_from_the_spec(self) -> None:
        # docs/tz/v3/json-examples/02_rate_response.json: страхование 0.18 %
        # от груза в 1 500 000 ₽ даёт 2 700 ₽.
        cargo = Money(150_000_000, "RUB")
        assert cargo.percentage(Decimal("0.18")) == Money(270_000, "RUB")

    def test_rounds_half_up_to_the_minor_unit(self) -> None:
        # 1005 копеек × 0.5 % = 5.025 копейки → 5.
        assert Money(1005, "RUB").percentage(Decimal("0.5")) == Money(5, "RUB")
        # 1010 копеек × 0.5 % = 5.05 копейки → 5; 1030 × 0.5 % = 5.15 → 5.
        assert Money(1030, "RUB").percentage(Decimal("0.5")) == Money(5, "RUB")
        # 1100 копеек × 0.5 % = 5.5 копейки → 6, а не 5 как дало бы округление
        # к чётному.
        assert Money(1100, "RUB").percentage(Decimal("0.5")) == Money(6, "RUB")

    def test_keeps_the_currency(self) -> None:
        assert Money(100_000, "USD").percentage(Decimal("1")).currency == "USD"

    def test_rejects_negative_rate(self) -> None:
        with pytest.raises(ValueError, match="отрицательной"):
            Money(100, "RUB").percentage(Decimal("-1"))


class TestMoneyImmutability:
    def test_amount_cannot_be_reassigned(self) -> None:
        # Сумма в снимке решения не должна меняться из-за чужой ссылки на неё.
        amount = Money(100, "RUB")
        with pytest.raises(AttributeError):
            amount.amount_minor = 200  # type: ignore[misc]

    def test_is_hashable_so_it_can_key_a_snapshot(self) -> None:
        assert len({Money(100, "RUB"), Money(100, "RUB"), Money(100, "USD")}) == 2


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


class TestRussianFormat:
    """Сумма, которую видит оператор (CLAUDE.md §6: интерфейс на русском).

    Формат обязан совпадать с ``Intl.NumberFormat('ru-RU')`` на фронте, иначе
    одна и та же сумма выглядит на экране двумя разными способами. Пробелы
    здесь неразрывные: сумма не должна разрываться переносом строки.
    """

    def test_matches_the_frontend_format(self) -> None:
        assert format_ru(Money(206_000, "RUB")) == "2\u00a0060,00\u00a0\u20bd"

    def test_groups_every_three_digits(self) -> None:
        assert format_ru(Money(123_456_789, "RUB")) == "1\u00a0234\u00a0567,89\u00a0\u20bd"

    def test_keeps_the_sign(self) -> None:
        assert format_ru(Money(-87_000, "RUB")) == "-870,00\u00a0\u20bd"

    def test_pads_amounts_smaller_than_the_major_unit(self) -> None:
        """Пять копеек — это «0,05», а не «,5» и не «5»."""
        assert format_ru(Money(5, "RUB")) == "0,05\u00a0\u20bd"
        assert format_ru(Money(0, "RUB")) == "0,00\u00a0\u20bd"

    def test_respects_the_currency_exponent(self) -> None:
        assert format_ru(Money(123_456, "JPY")) == "123\u00a0456\u00a0JPY"
        assert format_ru(Money(1_234_567, "KWD")) == "1\u00a0234,567\u00a0KWD"

    def test_unknown_currency_shows_its_code_not_an_invented_symbol(self) -> None:
        assert format_ru(Money(100, "XTS")).endswith("XTS")

    def test_str_stays_the_debug_form(self) -> None:
        """``str`` остаётся отладочным: иначе логи и тесты станут нечитаемыми."""
        assert str(Money(87_000, "RUB")) == "870.00 RUB"
