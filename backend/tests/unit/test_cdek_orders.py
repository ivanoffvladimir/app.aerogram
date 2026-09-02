"""Заказы СДЭК: создание, сверка «призраков», отмена, трекинг (неделя 6).

Фикстуры синтетические — см. tests/fixtures/cdek/README.md. Структура сверена
с сущностями официального SDK, но не заменяет прогон на боевом контуре.

Направление ошибок здесь дорогое, и тесты об этом. Ложное «не найден» на
сверке означает второй заказ у перевозчика с оплатой и вторым грузом; ложное
«принят» на создании — отправление, которого нет. Поэтому проверяется
не только успех, но и то, что каждый отказ остаётся отказом.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount, Party, Place, ShipmentRequest
from aerogram.carriers.cdek.adapter import CdekAdapter
from aerogram.carriers.cdek.client import SANDBOX_BASE_URL, CdekClient
from aerogram.carriers.cdek.orders import order_payload
from aerogram.carriers.status_map import normalize_status
from aerogram.shared.enums import CargoType
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cdek"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


class Exchange:
    """Что ушло к перевозчику и что он ответил. Один ответ на один вызов."""

    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "oauth" in str(request.url):
            return httpx.Response(200, json=load("oauth_ok"))
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def sent(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.last.content)
        return data


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> CdekAdapter:
    def factory(_: CarrierAccount) -> CdekClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL)
        return CdekClient(client_id="i", client_secret="s", http_client=inner)

    return CdekAdapter(client_factory=factory)


@pytest.fixture
def account() -> CarrierAccount:
    return CarrierAccount(
        account_id="1",
        carrier_code="cdek",
        mode="own_contract",
        credentials={"client_id": "i", "client_secret": "s"},
    )


def _request(*, insurance: bool = False, terminal: str | None = None) -> ShipmentRequest:
    return ShipmentRequest(
        number="AG-000123",
        service_code="136",
        tariff_code="136",
        sender=Party(
            city_fias_id="7b6de6a5-86d0-4735-b11a-499081111af8",
            city_name="Владивосток",
            carrier_city_code="75",
            address="ул. Светланская, 1",
            name="ООО Роспломба",
            contact_person="Иванов Иван",
            phone="+79140000000",
        ),
        recipient=Party(
            city_fias_id="0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
            city_name="Москва",
            carrier_city_code="44",
            terminal_code=terminal,
            address="ул. Тверская, 7",
            name="Петров Пётр",
            phone="+79160000000",
            email="petrov@example.com",
        ),
        places=(Place(weight_kg=Decimal("12.5"), length_cm=40, width_cm=30, height_cm=25),),
        declared_value=Money.from_major("480000.50", "RUB"),
        cargo_type=CargoType.EQUIPMENT,
        pickup=True,
        delivery_to_door=True,
        insurance=insurance,
        comment="Хрупкое",
    )


class TestCreatePayload:
    def test_units_and_identity_of_the_order(self) -> None:
        """Граммы и сантиметры, тип «доставка», наш номер как номер клиента.

        Ошибка в единице веса не даёт исключения — она даёт заказ на 12,5 г,
        который перевозчик взвесит сам и выставит счёт по факту.
        """
        payload = order_payload(_request())

        assert payload["type"] == 2
        assert payload["number"] == "AG-000123"
        assert payload["tariff_code"] == 136
        assert payload["packages"] == [
            {"number": "1", "weight": 12500, "length": 40, "width": 30, "height": 25}
        ]

    def test_locations_prefer_the_carrier_city_code(self) -> None:
        payload = order_payload(_request())

        assert payload["from_location"] == {"code": 75, "address": "ул. Светланская, 1"}
        assert payload["to_location"] == {"code": 44, "address": "ул. Тверская, 7"}

    def test_contacts_carry_a_person_a_phone_and_a_company(self) -> None:
        """Без имени и телефона СДЭК отказывает; компания — отдельно от лица."""
        payload = order_payload(_request())

        assert payload["sender"] == {
            "name": "Иванов Иван",
            "company": "ООО Роспломба",
            "phones": [{"number": "+79140000000"}],
        }
        assert payload["recipient"]["name"] == "Петров Пётр"
        assert payload["recipient"]["email"] == "petrov@example.com"
        assert "company" not in payload["recipient"]

    def test_insurance_is_a_service_with_the_declared_value_in_rubles(self) -> None:
        """Копейки → рубли ровно один раз и строкой: 480000.50, а не 480000.4999."""
        payload = order_payload(_request(insurance=True))

        assert payload["services"] == [{"code": "INSURANCE", "parameter": "480000.50"}]
        assert "services" not in order_payload(_request(insurance=False))

    def test_a_pickup_point_becomes_delivery_point(self) -> None:
        payload = order_payload(_request(terminal="MSK123"))

        assert payload["delivery_point"] == "MSK123"
        assert payload["comment"] == "Хрупкое"


class TestCreate:
    async def test_an_accepted_order_is_pending_until_the_waybill_number_arrives(
        self, account: CarrierAccount
    ) -> None:
        """В ответе только uuid: накладной ещё нет, и это ACCEPTED, а не CREATED."""
        exchange = Exchange(load("order_create_ok"))

        result = await _adapter(exchange.handler).create(_request(), account)

        assert result.external_id == "72753031-2801-4186-a091-0be58cedfee7"
        assert result.tracking_number is None
        assert result.is_pending is True
        assert exchange.last.method == "POST"
        assert exchange.last.url.path.endswith("/orders")
        assert exchange.sent["number"] == "AG-000123"

    async def test_a_rejected_order_carries_the_carrier_message(
        self, account: CarrierAccount
    ) -> None:
        """Отказ приходит телом при HTTP 200; статус здесь ничего не значит."""
        exchange = Exchange(load("order_create_invalid"), status=200)

        with pytest.raises(CarrierValidationError, match=r"recipient\.phones") as refusal:
            await _adapter(exchange.handler).create(_request(), account)

        assert refusal.value.carrier_code == "cdek"

    async def test_a_body_without_an_id_is_not_a_success(self, account: CarrierAccount) -> None:
        """Заказ мог создаться; второй запрос дал бы дубль. Это ошибка, а не пустота."""
        exchange = Exchange({"requests": [{"state": "ACCEPTED"}]})

        with pytest.raises(CarrierError, match="идентификатор"):
            await _adapter(exchange.handler).create(_request(), account)


class TestFindByNumber:
    async def test_an_existing_order_is_found_by_our_number(self, account: CarrierAccount) -> None:
        exchange = Exchange(load("order_info_ok"))

        found = await _adapter(exchange.handler).find_by_number("AG-000123", account)

        assert found is not None
        assert found.external_id == "72753031-2801-4186-a091-0be58cedfee7"
        assert found.tracking_number == "1106321645"
        assert found.is_pending is False
        assert exchange.last.method == "GET"
        assert exchange.last.url.params["im_number"] == "AG-000123"

    @pytest.mark.parametrize("status", [200, 404])
    async def test_not_found_is_none_whatever_the_http_status(
        self, account: CarrierAccount, status: int
    ) -> None:
        """«Не найден» распознаётся по коду в теле, а не по 404.

        SDK проверяет тело, а не статус, и контур отвечает по-разному.
        """
        exchange = Exchange(load("order_not_found"), status=status)

        assert await _adapter(exchange.handler).find_by_number("AG-999", account) is None

    async def test_any_other_error_stays_an_error(self, account: CarrierAccount) -> None:
        """Ложное «не найден» на сверке — второй заказ у перевозчика."""
        body = {
            "requests": [
                {"state": "INVALID", "errors": [{"code": "v2_bad_request", "message": "плохо"}]}
            ]
        }

        with pytest.raises(CarrierValidationError, match="плохо"):
            await _adapter(Exchange(body).handler).find_by_number("AG-1", account)


class TestTrack:
    async def test_statuses_become_events_our_map_understands(
        self, account: CarrierAccount
    ) -> None:
        exchange = Exchange(load("order_info_ok"))

        events = await _adapter(exchange.handler).track(
            "72753031-2801-4186-a091-0be58cedfee7", account
        )

        assert [e.status_raw for e in events] == [
            "ACCEPTED",
            "RECEIVED_AT_SHIPMENT_WAREHOUSE",
            "SENT_TO_RECIPIENT_CITY",
        ]
        assert all(not normalize_status("cdek", e.status_raw)[1] for e in events)
        assert exchange.last.url.path.endswith("/orders/72753031-2801-4186-a091-0be58cedfee7")

    async def test_a_deleted_status_is_skipped(self, account: CarrierAccount) -> None:
        """Так СДЭК отзывает ошибочно проставленные статусы."""
        events = await _adapter(Exchange(load("order_info_ok")).handler).track("u", account)

        assert "SENT_TO_TRANSIT_CITY" not in [e.status_raw for e in events]

    async def test_time_and_city_are_kept(self, account: CarrierAccount) -> None:
        events = await _adapter(Exchange(load("order_info_ok")).handler).track("u", account)

        assert events[1].occurred_at == datetime(2026, 9, 3, 7, 21, 5, tzinfo=UTC)
        assert events[1].city == "Владивосток"
        assert events[2].city == "Хабаровск"


class TestCancel:
    async def test_an_accepted_refusal(self, account: CarrierAccount) -> None:
        exchange = Exchange(load("order_refusal_ok"))

        result = await _adapter(exchange.handler).cancel(
            "72753031-2801-4186-a091-0be58cedfee7", account
        )

        assert result.accepted is True
        assert exchange.last.method == "POST"
        assert exchange.last.url.path.endswith(
            "/orders/72753031-2801-4186-a091-0be58cedfee7/refusal"
        )

    async def test_a_refusal_after_delivery_is_declined_with_a_reason(
        self, account: CarrierAccount
    ) -> None:
        """Отказ не исключение: домен покажет причину и оставит отправление как есть."""
        result = await _adapter(Exchange(load("order_refusal_rejected")).handler).cancel(
            "u", account
        )

        assert result.accepted is False
        assert result.message is not None
        assert "вручен" in result.message
