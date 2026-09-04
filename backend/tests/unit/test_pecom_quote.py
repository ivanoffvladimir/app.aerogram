"""Расчёт ПЭК: единицы, деньги, отказ по тарифу и ошибка с кодом 200.

Запросы в фикстурах — официальные примеры перевозчика, ответы собраны
по формату из его документации; см. `tests/fixtures/pecom/README.md`.
Сеть не используется.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount, Party, Place, QuoteRequest
from aerogram.carriers.pecom.adapter import PecomAdapter
from aerogram.carriers.pecom.client import SANDBOX_BASE_URL, PecomClient, pecom_error
from aerogram.carriers.pecom.mapping import (
    cargos_block,
    currency_from_code,
    money_from_response,
    to_metres,
    volume_m3,
)
from aerogram.carriers.pecom.quotes import build_quote_payload, tariff_types_for
from aerogram.shared.enums import CargoType, PriceSource
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pecom"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _account(mode: str = "own_contract") -> CarrierAccount:
    return CarrierAccount(
        account_id="acc-1",
        carrier_code="pecom",
        mode=mode,  # type: ignore[arg-type]
        credentials={"login": "user", "api_key": "key"},
    )


def _request(**overrides: object) -> QuoteRequest:
    defaults: dict[str, object] = {
        "sender": Party(
            city_fias_id=None,
            city_name="Москва",
            carrier_city_code="a678333f-2a2a-11e9-80ce-00155d713b38",
            address="Россия, Москва, Сормовский проезд, 7Ак2",
        ),
        "recipient": Party(
            city_fias_id=None,
            city_name="Санкт-Петербург",
            carrier_city_code="36cf9b60-a415-11dc-a911-000a5e19ccb4",
            address="Россия, Санкт-Петербург, Якорная, 2",
        ),
        "places": (Place(weight_kg=Decimal("46.32"), length_cm=54, width_cm=42, height_cm=16),),
        "declared_value": Money.from_major("15000", "RUB"),
        "cargo_type": CargoType.PARCEL,
        "pickup": False,
        "delivery_to_door": False,
    }
    defaults.update(overrides)
    return QuoteRequest(**defaults)  # type: ignore[arg-type]


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> PecomAdapter:
    def factory(_: CarrierAccount) -> PecomClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL)
        return PecomClient(login="user", api_key="key", http_client=inner)

    return PecomAdapter(client_factory=factory)


def _returns(
    body: dict[str, object], status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


class TestUnitsAgainstOfficialExamples:
    """Единицы сверяются с официальными примерами запросов ПЭК."""

    def test_official_example_uses_kilograms_metres_and_cubic_metres(self) -> None:
        example = load("request_terminal_to_terminal")
        cargo = example["cargos"][0]  # type: ignore[index]
        assert cargo["weight"] == 46.32  # кг
        assert cargo["volume"] == 0.41  # куб. м
        assert cargo["maxSize"] == 1.32  # м

    def test_our_block_matches_the_official_shape(self) -> None:
        """Габариты того же места из примера «с забором и доставкой»."""
        example = load("request_door_to_door")
        expected = example["cargos"][0]  # type: ignore[index]
        block = cargos_block(
            (Place(weight_kg=Decimal("46.32"), length_cm=54, width_cm=42, height_cm=16),)
        )
        assert block[0]["length"] == expected["length"]
        assert block[0]["width"] == expected["width"]
        assert block[0]["height"] == expected["height"]
        assert block[0]["weight"] == expected["weight"]

    def test_each_place_is_its_own_element(self) -> None:
        """У ПЭК места передаются списком, а не максимумом, как у ДЛ."""
        example = load("request_several_places")
        assert len(example["cargos"]) == 3  # type: ignore[arg-type]
        block = cargos_block(
            (
                Place(weight_kg=Decimal("5.2"), length_cm=25, width_cm=25, height_cm=25),
                Place(weight_kg=Decimal("46.32"), length_cm=54, width_cm=42, height_cm=16),
            )
        )
        assert len(block) == 2
        assert block[0]["weight"] == 5.2

    def test_conversions(self) -> None:
        assert to_metres(54) == Decimal("0.54")
        assert (
            volume_m3(Place(weight_kg=Decimal("1"), length_cm=100, width_cm=100, height_cm=100))
            == 1
        )
        with pytest.raises(ValueError, match="без грузовых мест"):
            cargos_block(())


class TestMoneyAndCurrency:
    def test_numbers_and_strings_are_both_accepted(self) -> None:
        assert money_from_response(5319, "RUB") == Money(531900, "RUB")
        assert money_from_response(446.6, "RUB") == Money(44660, "RUB")
        assert money_from_response("5319", "RUB") == Money(531900, "RUB")

    def test_missing_cost_is_not_zero(self) -> None:
        assert money_from_response(None, "RUB") is None
        assert money_from_response(True, "RUB") is None

    def test_currency_comes_from_the_numeric_iso_code(self) -> None:
        assert currency_from_code("643") == "RUB"
        assert currency_from_code("398") == "KZT"

    def test_unknown_currency_is_refused_not_guessed(self) -> None:
        """Сумма в чужой валюте, посчитанная как рублёвая, выиграет сравнение."""
        assert currency_from_code("999") is None
        assert currency_from_code(None) is None


class TestPayload:
    def test_default_tariff_is_ltl_auto(self) -> None:
        assert tariff_types_for(_request()) == (3,)

    def test_express_auto_is_never_requested(self) -> None:
        """Документация прямо предупреждает, что тариф 5 метод не считает."""
        assert tariff_types_for(_request(extras={"tariff_types": [5]})) == (3,)

    def test_several_tariffs_travel_in_one_call(self) -> None:
        assert tariff_types_for(_request(extras={"tariff_types": [3, 1]})) == (3, 1)

    def test_warehouses_and_currency(self) -> None:
        payload = build_quote_payload(_request(), (3,))
        assert payload["currencyCode"] == "643"
        assert payload["senderWarehouseId"] == "a678333f-2a2a-11e9-80ce-00155d713b38"
        assert payload["types"] == [3]

    def test_pickup_and_delivery_only_when_asked(self) -> None:
        """Лишняя услуга завышает цену и проигрывает сравнение по нашей вине."""
        plain = build_quote_payload(_request(), (3,))
        assert plain["isPickUp"] is False
        assert "pickup" not in plain

        with_door = build_quote_payload(_request(pickup=True, delivery_to_door=True), (3,))
        assert with_door["isPickUp"] is True
        assert with_door["pickup"] == {"address": "Россия, Москва, Сормовский проезд, 7Ак2"}
        assert with_door["delivery"] == {"address": "Россия, Санкт-Петербург, Якорная, 2"}

    def test_insurance_carries_the_declared_value(self) -> None:
        payload = build_quote_payload(_request(insurance=True), (3,))
        assert payload["isInsurance"] is True
        assert payload["isInsurancePrice"] == 15000.0


class TestErrorEnvelope:
    def test_logical_error_is_recognised(self) -> None:
        message = pecom_error(load("error_logical"))
        assert message is not None
        assert "Не указан обязательный параметр" in message

    def test_calculation_error_is_recognised(self) -> None:
        message = pecom_error(load("error_calculation"))
        assert message == "Не удалось определить филиал по адресу отправления"

    def test_success_has_no_error(self) -> None:
        assert pecom_error(load("calculate_ok")) is None


@pytest.mark.anyio
class TestQuote:
    async def test_successful_tariff_becomes_a_quote(self) -> None:
        adapter = _adapter(_returns(load("calculate_ok")))
        quotes = await adapter.quote(_request(extras={"tariff_types": [3, 1]}), _account())

        assert len(quotes) == 1
        quote = quotes[0]
        assert quote.service_code == "3"
        assert quote.service_name == "ПЭК:LTL Авто"
        # costTotal = 5319 рублей → 531900 копеек.
        assert quote.price == Money(531900, "RUB")
        assert quote.transit_days_min == 3
        assert quote.price_source is PriceSource.OWN_CONTRACT

    async def test_refused_tariff_is_not_an_offer(self) -> None:
        """Тариф с hasError приходит без цены — ноль вывел бы его первым."""
        adapter = _adapter(_returns(load("calculate_ok")))
        quotes = await adapter.quote(_request(extras={"tariff_types": [3, 1]}), _account())
        assert [q.service_code for q in quotes] == ["3"]

    async def test_nested_services_are_all_in_the_breakdown(self) -> None:
        """Стоимость вложенных услуг НЕ входит в родительскую — так в документации."""
        adapter = _adapter(_returns(load("calculate_ok")))
        quote = (await adapter.quote(_request(), _account()))[0]
        assert quote.price_breakdown["Перевозка"] == Money(526900, "RUB")
        assert quote.price_breakdown["Страхование"] == Money(5000, "RUB")
        assert quote.price_breakdown["Погрузо-разгрузочные работы"] == Money(25000, "RUB")

    async def test_a_logical_error_with_status_200_is_still_an_error(self) -> None:
        """Главная ловушка контракта ПЭК: успешный код и ошибка в теле."""
        adapter = _adapter(_returns(load("error_logical"), status=200))
        with pytest.raises(CarrierError, match="обязательный параметр"):
            await adapter.quote(_request(), _account())

    async def test_calculation_error_is_not_a_500(self) -> None:
        adapter = _adapter(_returns(load("error_calculation")))
        with pytest.raises(CarrierError, match="филиал"):
            await adapter.quote(_request(), _account())

    async def test_unknown_currency_refuses_the_whole_answer(self) -> None:
        body = dict(load("calculate_ok"))
        body["currencyCode"] = "999"
        adapter = _adapter(_returns(body))
        with pytest.raises(CarrierError, match="неизвестный код валюты"):
            await adapter.quote(_request(), _account())

    async def test_credentials_are_required(self) -> None:
        with pytest.raises(CarrierValidationError, match="не заданы"):
            PecomAdapter()._default_client(
                CarrierAccount(
                    account_id="a",
                    carrier_code="pecom",
                    mode="own_contract",
                    credentials={"login": "user"},
                )
            )
