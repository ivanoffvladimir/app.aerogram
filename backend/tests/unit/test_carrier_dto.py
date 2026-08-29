"""DTO контракта адаптера: неизменяемость и производные величины."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from aerogram.carriers.base import CarrierAccount, Party, Place, QuoteRequest, RawEvent
from aerogram.shared.enums import CargoType, PriceSource


def _party(city: str = "Москва") -> Party:
    return Party(city_fias_id="0c5b2444-70a0-4932-980c-b4dc0d3f02b5", city_name=city)


def _request(places: tuple[Place, ...]) -> QuoteRequest:
    return QuoteRequest(
        sender=_party("Владивосток"),
        recipient=_party("Москва"),
        places=places,
        declared_value=Decimal("480000.00"),
        cargo_type=CargoType.EQUIPMENT,
        pickup=True,
        delivery_to_door=True,
    )


class TestImmutability:
    def test_place_cannot_be_mutated(self) -> None:
        # Мутация DTO между слоями — источник ошибок, которые агент вносит охотнее
        # всего (раздел 8.1 ТЗ), поэтому все DTO frozen.
        place = Place(weight_kg=Decimal("12"), length_cm=40, width_cm=30, height_cm=25)
        with pytest.raises(FrozenInstanceError):
            place.weight_kg = Decimal("1")  # type: ignore[misc]

    def test_quote_request_cannot_be_mutated(self) -> None:
        request = _request((Place(Decimal("12"), 40, 30, 25),))
        with pytest.raises(FrozenInstanceError):
            request.pickup = False  # type: ignore[misc]


class TestTotals:
    def test_total_weight_sums_places(self) -> None:
        request = _request(
            (
                Place(Decimal("12.5"), 40, 30, 25),
                Place(Decimal("3.25"), 20, 20, 20),
            )
        )
        assert request.total_weight_kg == Decimal("15.75")

    def test_total_weight_is_decimal_not_float(self) -> None:
        request = _request((Place(Decimal("0.1"), 10, 10, 10), Place(Decimal("0.2"), 10, 10, 10)))
        assert request.total_weight_kg == Decimal("0.3")


class TestCarrierAccount:
    def test_own_contract_maps_to_own_price_source(self) -> None:
        account = CarrierAccount(
            account_id="1", carrier_code="cdek", mode="own_contract", credentials={}
        )
        assert account.price_source is PriceSource.OWN_CONTRACT

    def test_aerogram_mode_maps_to_platform_price_source(self) -> None:
        account = CarrierAccount(
            account_id="1", carrier_code="cdek", mode="aerogram", credentials={}
        )
        assert account.price_source is PriceSource.AEROGRAM


class TestRawEvent:
    def test_dedup_key_is_stable(self) -> None:
        moment = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
        first = RawEvent(occurred_at=moment, status_raw="DELIVERED", city="Москва")
        second = RawEvent(occurred_at=moment, status_raw="DELIVERED", city="Москва", comment="иное")
        # Одно и то же событие, пришедшее вебхуком и опросом, не должно
        # попасть в ленту дважды — комментарий на это не влияет.
        assert first.dedup_key() == second.dedup_key()

    def test_dedup_key_differs_for_different_events(self) -> None:
        moment = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
        first = RawEvent(occurred_at=moment, status_raw="DELIVERED", city="Москва")
        second = RawEvent(occurred_at=moment, status_raw="IN_TRANSIT", city="Москва")
        assert first.dedup_key() != second.dedup_key()
