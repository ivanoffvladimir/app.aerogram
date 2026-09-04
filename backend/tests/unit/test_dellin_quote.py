"""Расчёт Деловых Линий: единицы, деньги, сроки и договорная цена.

Ответ калькулятора взят из официальной OpenAPI перевозчика — см.
`tests/fixtures/dellin/README.md`. Сеть не используется: транспорт
подменяется целиком, боевой контур у Деловых Линий единственный.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount, Party, Place, QuoteRequest
from aerogram.carriers.dellin.adapter import DellinAdapter
from aerogram.carriers.dellin.client import BASE_URL, DellinClient
from aerogram.carriers.dellin.mapping import (
    cargo_block,
    money_from_response,
    parse_carrier_date,
    to_metres,
    total_volume_m3,
)
from aerogram.carriers.dellin.quotes import build_quote_payload, delivery_types_for
from aerogram.shared.enums import CargoType, PriceSource
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dellin"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _account(mode: str = "own_contract", **credentials: str) -> CarrierAccount:
    return CarrierAccount(
        account_id="acc-1",
        carrier_code="dellin",
        mode=mode,  # type: ignore[arg-type]
        credentials={"appkey": "key", "pat": "dl-api-token", **credentials},
    )


def _request(**overrides: object) -> QuoteRequest:
    defaults: dict[str, object] = {
        "sender": Party(
            city_fias_id=None,
            city_name="Санкт-Петербург",
            carrier_city_code="7800000000000000000000000",
        ),
        "recipient": Party(
            city_fias_id=None, city_name="Чита", carrier_city_code="7500000100000000000000000"
        ),
        "places": (Place(weight_kg=Decimal("46.32"), length_cm=54, width_cm=42, height_cm=16),),
        "declared_value": Money.from_major("15000", "RUB"),
        "cargo_type": CargoType.PARCEL,
        "pickup": True,
        "delivery_to_door": True,
    }
    defaults.update(overrides)
    return QuoteRequest(**defaults)  # type: ignore[arg-type]


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response], **credentials: str
) -> DellinAdapter:
    def factory(acc: CarrierAccount) -> DellinClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        return DellinClient(
            appkey=acc.credentials.get("appkey", "key"),
            pat=acc.credentials.get("pat") or None,
            login=acc.credentials.get("login") or None,
            password=acc.credentials.get("password") or None,
            http_client=inner,
        )

    return DellinAdapter(client_factory=factory)


def _handler_for(calculator_body: dict[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if "auth/login" in str(request.url):
            return httpx.Response(200, json=load("login_ok"))
        return httpx.Response(200, json=calculator_body)

    return handler


class TestUnits:
    """Ошибка в единицах не даёт исключения — она даёт цену в сто раз другую."""

    def test_centimetres_become_metres(self) -> None:
        assert to_metres(54) == Decimal("0.54")
        assert to_metres(100) == Decimal("1")

    def test_volume_is_cubic_metres(self) -> None:
        places = (Place(weight_kg=Decimal("1"), length_cm=100, width_cm=100, height_cm=100),)
        assert total_volume_m3(places) == Decimal("1")

    def test_cargo_block_carries_maxima_and_totals(self) -> None:
        """Габариты у ДЛ — максимум по местам, вес и объём — суммы."""
        places = (
            Place(weight_kg=Decimal("5.2"), length_cm=25, width_cm=25, height_cm=25),
            Place(weight_kg=Decimal("46.32"), length_cm=54, width_cm=42, height_cm=16),
        )
        block = cargo_block(places, Money.from_major("1000", "RUB"), insure=False)
        assert block["quantity"] == 2
        assert block["length"] == 0.54  # самое длинное место, метры
        assert block["weight"] == 46.32  # самое тяжёлое место, килограммы
        assert block["totalWeight"] == 51.52
        # 25×25×25 = 15 625 см³ и 54×42×16 = 36 288 см³ → 0.051913 м³.
        assert block["totalVolume"] == 0.051913
        assert "insurance" not in block

    def test_declared_value_travels_as_a_string_in_roubles(self) -> None:
        """statedValue объявлена строкой и в спеке, и в примерах."""
        block = cargo_block(
            (Place(weight_kg=Decimal("1"), length_cm=10, width_cm=10, height_cm=10),),
            Money.from_major("15000.50", "RUB"),
            insure=True,
        )
        assert block["insurance"] == {"statedValue": "15000.50", "term": False}

    def test_request_without_places_is_refused(self) -> None:
        with pytest.raises(ValueError, match="без грузовых мест"):
            cargo_block((), Money.zero("RUB"), insure=False)


class TestMoneyParsing:
    """Перевозчик отдаёт цену и числом, и строкой — спека расходится с примером."""

    def test_number_and_string_give_the_same_money(self) -> None:
        assert money_from_response(475) == Money.from_major("475", "RUB")
        assert money_from_response("475") == Money.from_major("475", "RUB")
        assert money_from_response(320.0) == Money.from_major("320", "RUB")
        assert money_from_response("1680,55") == Money.from_major("1680.55", "RUB")

    def test_missing_price_is_not_zero(self) -> None:
        """Ноль вместо отсутствующей цены вывел бы строку первой как самую дешёвую."""
        assert money_from_response(None) is None
        assert money_from_response("") is None
        assert money_from_response(True) is None
        assert money_from_response({"price": 1}) is None

    def test_dates_come_in_two_shapes(self) -> None:
        assert parse_carrier_date("2019-11-26") is not None
        assert parse_carrier_date("2019-11-28 00:00:00") is not None
        assert parse_carrier_date(None) is None
        assert parse_carrier_date("не дата") is None


class TestPayload:
    def test_door_to_door_asks_for_addresses(self) -> None:
        payload = build_quote_payload(_request(), "auto")
        delivery = payload["delivery"]
        assert delivery["deliveryType"] == {"type": "auto"}  # type: ignore[index]
        assert delivery["derival"]["variant"] == "address"  # type: ignore[index]
        assert delivery["arrival"]["variant"] == "address"  # type: ignore[index]

    def test_terminal_to_terminal_carries_kladr_codes(self) -> None:
        payload = build_quote_payload(_request(pickup=False, delivery_to_door=False), "auto")
        derival = payload["delivery"]["derival"]  # type: ignore[index]
        assert derival["variant"] == "terminal"
        assert derival["city"] == "7800000000000000000000000"

    def test_terminal_code_wins_over_city(self) -> None:
        sender = Party(
            city_fias_id=None,
            city_name="Санкт-Петербург",
            carrier_city_code="7800000000000000000000000",
            terminal_code="104",
        )
        payload = build_quote_payload(_request(sender=sender, pickup=False), "auto")
        derival = payload["delivery"]["derival"]  # type: ignore[index]
        assert derival["terminalID"] == "104"
        assert "city" not in derival

    def test_unknown_delivery_types_fall_back_to_auto(self) -> None:
        assert delivery_types_for(_request()) == ("auto",)
        assert delivery_types_for(_request(extras={"delivery_types": ["express", "auto"]})) == (
            "express",
            "auto",
        )
        assert delivery_types_for(_request(extras={"delivery_types": ["телепорт"]})) == ("auto",)


@pytest.mark.anyio
class TestQuote:
    async def test_official_example_becomes_a_quote(self) -> None:
        adapter = _adapter(_handler_for(load("calculator_ok")))
        quotes = await adapter.quote(_request(), _account())

        assert len(quotes) == 1
        quote = quotes[0]
        assert quote.service_code == "auto"
        assert quote.service_name == "Автодоставка"
        # data.price = 1680 рублей → 168000 копеек. Ни округления, ни float.
        assert quote.price == Money(168000, "RUB")
        assert quote.price_source is PriceSource.OWN_CONTRACT
        # Плечи попадают в расшифровку: забор 475 руб., страхование 250 руб.
        assert quote.price_breakdown["pickup"] == Money(47500, "RUB")
        assert quote.price_breakdown["insurance"] == Money(25000, "RUB")

    async def test_total_price_is_taken_not_the_available_types(self) -> None:
        """availableDeliveryTypes.auto = 480 в том же ответе — оно не используется."""
        adapter = _adapter(_handler_for(load("calculator_ok")))
        quotes = await adapter.quote(_request(), _account())
        assert quotes[0].price != Money.from_major("480", "RUB")

    async def test_transit_days_come_from_order_dates(self) -> None:
        """Забор 2019-11-26, выдача на терминале 2019-11-28 и 11-29."""
        adapter = _adapter(_handler_for(load("calculator_ok")))
        quotes = await adapter.quote(_request(delivery_to_door=False), _account())
        assert quotes[0].transit_days_min == 2
        assert quotes[0].transit_days_max == 3
        assert quotes[0].promised_delivery_date is not None

    async def test_contract_price_is_not_an_offer(self) -> None:
        """Везти согласны, цену назовёт менеджер — это не котировка."""
        adapter = _adapter(_handler_for(load("calculator_contract_price")))
        with pytest.raises(CarrierValidationError, match="договорной цене"):
            await adapter.quote(_request(), _account())

    async def test_carrier_error_is_not_a_500(self) -> None:
        adapter = _adapter(_handler_for(load("error_login")))
        with pytest.raises(CarrierError) as exc:
            await adapter.quote(_request(), _account())
        assert "обязательный параметр" in str(exc.value)

    async def test_own_contract_without_login_is_refused(self) -> None:
        """Публичный тариф, подписанный как цена по договору, — ложь в деньгах."""
        adapter = _adapter(_handler_for(load("calculator_ok")))
        account = CarrierAccount(
            account_id="acc-2",
            carrier_code="dellin",
            mode="own_contract",
            credentials={"appkey": "key"},
        )
        with pytest.raises(CarrierValidationError, match="публичный тариф"):
            await adapter.quote(_request(), account)

    async def test_platform_mode_works_without_login(self) -> None:
        """Без договора клиента публичный тариф — законный ответ."""
        adapter = _adapter(_handler_for(load("calculator_ok")))
        account = CarrierAccount(
            account_id="acc-3",
            carrier_code="dellin",
            mode="aerogram",
            credentials={"appkey": "key"},
        )
        quotes = await adapter.quote(_request(), account)
        assert quotes[0].price_source is PriceSource.AEROGRAM

    async def test_several_delivery_types_give_several_quotes(self) -> None:
        adapter = _adapter(_handler_for(load("calculator_ok")))
        quotes = await adapter.quote(
            _request(extras={"delivery_types": ["auto", "express"]}), _account()
        )
        assert [q.service_code for q in quotes] == ["auto", "express"]
