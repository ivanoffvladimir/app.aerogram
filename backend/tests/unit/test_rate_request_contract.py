"""Разбор запроса расчёта по контракту и вычисления вокруг дедлайна."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from aerogram.rating.schemas import RateRequestIn
from aerogram.rating.service import _deadline_gap, _end_of_day, _mm_to_cm, _offer_source
from aerogram.shared.enums import CargoType, OfferSource, PriceSource, RoutingStrategy

MINIMAL = {
    "origin": {"country": "RU", "city": "Москва", "address_line": "ул. Тверская, 1"},
    "destination": {"country": "RU", "city": "Владивосток", "address_line": "ул. Морская, 2"},
    "packages": [{"weight_grams": 1000}],
    "cargo_value": {"amount_minor": 100_000, "currency": "RUB"},
    "strategy": "optimal",
}


class TestRequestDefaults:
    def test_minimal_request_from_the_contract_is_accepted(self) -> None:
        payload = RateRequestIn.model_validate(MINIMAL)
        assert payload.strategy is RoutingStrategy.OPTIMAL
        assert payload.cargo_type is CargoType.PARCEL

    def test_dimensions_are_optional(self) -> None:
        """Контракт требует только вес: габариты знает не всякий клиент."""
        payload = RateRequestIn.model_validate(MINIMAL)
        assert payload.packages[0].length_mm is None

    def test_weight_converts_to_kilograms_without_float(self) -> None:
        payload = RateRequestIn.model_validate({**MINIMAL, "packages": [{"weight_grams": 12_345}]})
        assert str(payload.packages[0].weight_kg) == "12.345"


class TestAdditionalServices:
    def test_empty_list_means_terminal_to_terminal(self) -> None:
        """Пустой список — отсутствие доплат, а не доставка до двери.

        Так этот список читается в примере ТЗ: door_delivery перечислен явно,
        значит без него двери нет.
        """
        payload = RateRequestIn.model_validate(MINIMAL)
        assert payload.pickup is False
        assert payload.delivery_to_door is False
        assert payload.insurance is False

    def test_listed_services_are_honoured(self) -> None:
        payload = RateRequestIn.model_validate(
            {**MINIMAL, "additional_services": ["door_delivery", "insurance"]}
        )
        assert payload.delivery_to_door is True
        assert payload.insurance is True
        assert payload.pickup is False

    def test_unknown_service_is_rejected_not_ignored(self) -> None:
        """Молчаливое игнорирование дало бы расчёт без страхования там,
        где страхование просили, — и заметили бы это при страховом случае."""
        with pytest.raises(ValidationError, match="неизвестные дополнительные услуги"):
            RateRequestIn.model_validate({**MINIMAL, "additional_services": ["insurence"]})


class TestTimezoneDiscipline:
    def test_naive_deadline_is_rejected_not_guessed(self) -> None:
        """Достроить зону значило бы выбрать её за клиента.

        Для Москва → Владивосток разница в семь часов решает, уложился ли
        перевозчик в срок, а без зоны сравнение раньше роняло весь расчёт.
        """
        with pytest.raises(ValidationError, match="часовой пояс"):
            RateRequestIn.model_validate({**MINIMAL, "deadline": "2026-09-05T12:00:00"})

    def test_naive_ship_at_is_rejected_too(self) -> None:
        with pytest.raises(ValidationError, match="часовой пояс"):
            RateRequestIn.model_validate({**MINIMAL, "ship_at": "2026-09-01T10:00:00"})

    def test_offset_aware_deadline_is_accepted(self) -> None:
        payload = RateRequestIn.model_validate({**MINIMAL, "deadline": "2026-09-05T12:00:00+03:00"})
        assert payload.deadline is not None and payload.deadline.tzinfo is not None


class TestMillimetresToCentimetres:
    def test_rounds_up_so_the_tariff_is_not_understated(self) -> None:
        # 305 мм это 31 см для тарифа: округление вниз занизило бы объёмный
        # вес и, значит, цену.
        assert _mm_to_cm(305) == 31
        assert _mm_to_cm(300) == 30

    def test_missing_dimension_becomes_one_centimetre(self) -> None:
        # Ноль запрещён проверкой объёмного веса, поэтому не ноль.
        assert _mm_to_cm(None) == 1

    def test_sub_centimetre_does_not_collapse_to_zero(self) -> None:
        assert _mm_to_cm(4) == 1


class TestEndOfDay:
    def test_promised_day_becomes_the_end_of_that_day(self) -> None:
        """Взять начало дня значило бы обещать за перевозчика больше,
        чем он сказал."""
        eta = _end_of_day(date(2026, 9, 5), "Europe/Moscow")
        assert eta is not None
        assert (eta.hour, eta.minute) == (23, 59)
        assert eta.utcoffset() is not None and eta.utcoffset().total_seconds() == 3 * 3600

    def test_destination_timezone_decides_the_moment(self) -> None:
        """Конец дня во Владивостоке наступает на семь часов раньше московского."""
        vladivostok = _end_of_day(date(2026, 9, 5), "Asia/Vladivostok")
        moscow = _end_of_day(date(2026, 9, 5), "Europe/Moscow")
        assert vladivostok is not None and moscow is not None
        assert vladivostok < moscow

    def test_unknown_timezone_falls_back_to_utc_instead_of_failing(self) -> None:
        eta = _end_of_day(date(2026, 9, 5), "Nowhere/Nothing")
        assert eta is not None and eta.tzinfo is UTC

    def test_no_promised_day_gives_no_eta(self) -> None:
        assert _end_of_day(None, "Europe/Moscow") is None


class TestDeadlineGap:
    def test_margin_when_the_offer_fits(self) -> None:
        eta = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
        deadline = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        assert _deadline_gap(eta, deadline) == (86_400, 0)

    def test_lateness_when_it_does_not(self) -> None:
        eta = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
        deadline = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        assert _deadline_gap(eta, deadline) == (0, 86_400)

    def test_both_are_never_negative(self) -> None:
        """Отрицательный запас читался бы двусмысленно."""
        eta = datetime(2026, 9, 6, tzinfo=UTC)
        margin, lateness = _deadline_gap(eta, datetime(2026, 9, 5, tzinfo=UTC))
        assert margin is not None and lateness is not None
        assert margin >= 0 and lateness >= 0

    def test_without_a_deadline_there_is_nothing_to_measure(self) -> None:
        assert _deadline_gap(datetime(2026, 9, 6, tzinfo=UTC), None) == (None, None)


class TestOfferSource:
    def test_internal_price_source_maps_to_the_contract(self) -> None:
        assert _offer_source(PriceSource.OWN_CONTRACT) is OfferSource.CLIENT_CONTRACT
        assert _offer_source(PriceSource.AEROGRAM) is OfferSource.LOGISTICS_OS

    def test_public_tariff_has_no_contract_value_and_is_not_invented(self) -> None:
        """Публичный расчёт ПЭК не является ни договором клиента, ни тарифом
        платформы. Расхождение вынесено в docs/status.md; выдумывать значение
        вместо решения человека нельзя."""
        assert _offer_source(PriceSource.PUBLIC) is None
        assert _offer_source(None) is None
