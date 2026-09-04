"""Заказы Деловых Линий: оформление, поиск, трекинг и печатная форма.

История статусов взята из официальной спеки перевозчика; остальные фикстуры
собраны по её схемам — см. `tests/fixtures/dellin/README.md`. Сеть не
используется: боевой контур у Деловых Линий единственный.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount, Party, Place, ShipmentRequest
from aerogram.carriers.dellin.adapter import DellinAdapter
from aerogram.carriers.dellin.client import BASE_URL, DellinClient
from aerogram.carriers.dellin.orders import (
    ORDERS_PATH,
    PRINTABLE_PATH,
    REQUEST_PATH,
    STATUSES_HISTORY_PATH,
    create_payload,
    parse_statuses,
    waybill_uid,
)
from aerogram.carriers.status_map import load_status_map
from aerogram.shared.enums import CargoType, LabelFormat, ShipmentStatus
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dellin"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _account() -> CarrierAccount:
    return CarrierAccount(
        account_id="acc-1",
        carrier_code="dellin",
        mode="own_contract",
        credentials={"appkey": "key", "pat": "dl-api-token"},
    )


def _shipment(**overrides: object) -> ShipmentRequest:
    defaults: dict[str, object] = {
        "number": "AG-2026-000123",
        "service_code": "auto",
        "tariff_code": "auto",
        "sender": Party(
            city_fias_id=None,
            city_name="Санкт-Петербург",
            carrier_city_code="7800000000000000000000000",
            address="Кожевенная линия, 40",
            name="ООО Ромашка",
            contact_person="Иванов И.И.",
            phone="79990000000",
            inn="7801234567",
        ),
        "recipient": Party(
            city_fias_id=None,
            city_name="Чита",
            carrier_city_code="7500000100000000000000000",
            address="Сухая Падь, 3",
            name="ИП Петров",
            phone="79991111111",
        ),
        "places": (Place(weight_kg=Decimal("46.32"), length_cm=54, width_cm=42, height_cm=16),),
        "declared_value": Money.from_major("15000", "RUB"),
        "cargo_type": CargoType.PARCEL,
        "pickup": True,
        "delivery_to_door": True,
        "extras": {"freight_uid": "0x82e6000423b423b711da7d15445d42cb"},
    }
    defaults.update(overrides)
    return ShipmentRequest(**defaults)  # type: ignore[arg-type]


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> DellinAdapter:
    def factory(acc: CarrierAccount) -> DellinClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        return DellinClient(appkey="key", pat="t", http_client=inner)

    return DellinAdapter(client_factory=factory)


def _routes(**by_path: dict[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "auth/login" in path:
            return httpx.Response(200, json=load("login_ok"))
        body = by_path.get(path)
        if body is None:
            return httpx.Response(404, json={"errors": [{"title": f"нет маршрута {path}"}]})
        return httpx.Response(200, json=body)

    return handler


class TestCreatePayload:
    def test_our_number_travels_so_ghosts_can_be_found(self) -> None:
        """Сверка «призраков» ищет заказ по нашему номеру (FR-2.5)."""
        payload = create_payload(_shipment(), freight_uid="0xfreight")
        assert payload["cargoCode"] == "AG-2026-000123"
        assert payload["orderNumber"] == "AG-2026-000123"

    def test_members_are_inline_not_from_someone_elses_address_book(self) -> None:
        payload = create_payload(_shipment(), freight_uid="0xfreight")
        sender = payload["members"]["sender"]  # type: ignore[index]
        assert sender["counteragent"] == {"name": "ООО Ромашка", "inn": "7801234567"}
        assert sender["contactPersons"] == [{"name": "Иванов И.И."}]
        assert sender["phoneNumbers"] == [{"number": "79990000000"}]
        # Чужую адресную книгу мы не заполняем.
        assert "save" not in sender["counteragent"]

    def test_freight_uid_lands_in_cargo(self) -> None:
        payload = create_payload(_shipment(), freight_uid="0xfreight")
        assert payload["cargo"]["freightUID"] == "0xfreight"  # type: ignore[index]

    def test_unknown_delivery_type_falls_back_to_auto(self) -> None:
        payload = create_payload(
            _shipment(extras={"freight_uid": "0xf", "delivery_type": "телепорт"}),
            freight_uid="0xf",
        )
        assert payload["delivery"]["deliveryType"] == {"type": "auto"}  # type: ignore[index]


class TestStatusMap:
    def test_codes_from_the_spec_are_mapped(self) -> None:
        status_map = load_status_map("dellin")
        assert status_map.normalize("waiting") == (ShipmentStatus.ACCEPTED, False)
        assert status_map.normalize("inway") == (ShipmentStatus.IN_TRANSIT, False)
        assert status_map.normalize("delivered") == (ShipmentStatus.DELIVERED, False)

    def test_unknown_code_does_not_break_the_feed(self) -> None:
        """Перечень статусов в спеке неполон — справочник недостижим."""
        status, unmapped = load_status_map("dellin").normalize("совершенно новый статус")
        assert status is ShipmentStatus.IN_TRANSIT
        assert unmapped is True


class TestStatusParsing:
    def test_official_history_becomes_events(self) -> None:
        events = parse_statuses(load("statuses_history_ok"))
        assert [e.status_raw for e in events] == ["waiting", "inway"]
        assert events[0].comment == "Ожидает сдачи на терминал"
        # Время со смещением: 2023-01-12T15:52:40+03:00.
        assert events[0].occurred_at.tzinfo is not None
        assert events[0].occurred_at < events[1].occurred_at

    def test_rows_without_time_or_status_are_skipped_not_faked(self) -> None:
        body = {"data": {"statusHistory": {"1": [{"state": "inway"}, {"stateDate": "2026-01-01"}]}}}
        assert parse_statuses(body) == []

    def test_missing_history_is_an_empty_feed(self) -> None:
        assert parse_statuses({"data": {}}) == []
        assert parse_statuses({}) == []


class TestWaybillUid:
    def test_shipping_document_is_picked_not_the_request(self) -> None:
        assert waybill_uid(load("orders_found")) == "0xad339ac31247666145816f2aeb4935ab"

    def test_no_orders_means_no_uid(self) -> None:
        assert waybill_uid(load("orders_empty")) is None


@pytest.mark.anyio
class TestAdapter:
    async def test_created_order_is_pending(self) -> None:
        """Перевозчик отдаёт номер заявки; номер заказа появится позже."""
        adapter = _adapter(_routes(**{REQUEST_PATH: load("request_created")}))
        result = await adapter.create(_shipment(), _account())
        assert result.external_id == "400275691"
        assert result.tracking_number == "1345678"
        assert result.is_pending is True

    async def test_create_without_freight_uid_is_refused(self) -> None:
        """Умолчание хуже отказа: перевозчик повезёт не тот характер груза."""
        adapter = _adapter(_routes(**{REQUEST_PATH: load("request_created")}))
        with pytest.raises(CarrierValidationError, match="характер груза"):
            await adapter.create(_shipment(extras={}), _account())

    async def test_carrier_error_on_create_is_not_a_500(self) -> None:
        adapter = _adapter(_routes(**{REQUEST_PATH: load("error_login")}))
        with pytest.raises(CarrierError, match="обязательный параметр"):
            await adapter.create(_shipment(), _account())

    async def test_find_by_our_number(self) -> None:
        adapter = _adapter(_routes(**{ORDERS_PATH: load("orders_found")}))
        found = await adapter.find_by_number("AG-2026-000123", _account())
        assert found is not None
        assert found.external_id == "400275691"
        assert found.price_actual == Money(168000, "RUB")
        assert found.is_pending is False

    async def test_missing_order_means_creation_really_failed(self) -> None:
        """Для сверки «призраков» пустой журнал — значимый ответ, а не ошибка."""
        adapter = _adapter(_routes(**{ORDERS_PATH: load("orders_empty")}))
        assert await adapter.find_by_number("AG-2026-000123", _account()) is None

    async def test_track_returns_the_feed(self) -> None:
        adapter = _adapter(_routes(**{STATUSES_HISTORY_PATH: load("statuses_history_ok")}))
        events = await adapter.track("400267443", _account())
        assert len(events) == 2
        assert events[-1].status_raw == "inway"

    async def test_label_resolves_the_waybill_then_prints_it(self) -> None:
        adapter = _adapter(
            _routes(**{ORDERS_PATH: load("orders_found"), PRINTABLE_PATH: load("printable_ok")})
        )
        label = await adapter.label("400275691", LabelFormat.PDF_A4, _account())
        assert label.is_pending is False
        assert label.content is not None
        assert label.content.startswith(b"%PDF")
        assert label.external_ref == "0xad339ac31247666145816f2aeb4935ab"

    async def test_label_is_pending_while_the_waybill_does_not_exist(self) -> None:
        """Пока заявка не обработана, накладной нет — это не ошибка (FR-4.5)."""
        adapter = _adapter(_routes(**{ORDERS_PATH: load("orders_empty")}))
        label = await adapter.label("400275691", LabelFormat.PDF_A4, _account())
        assert label.is_pending is True
        assert label.content is None

    async def test_only_pdf_is_offered(self) -> None:
        adapter = _adapter(_routes(**{ORDERS_PATH: load("orders_found")}))
        with pytest.raises(CarrierValidationError, match="только в PDF"):
            await adapter.label("400275691", LabelFormat.ZPL, _account())

    async def test_webhook_shape_is_unknown_and_does_not_crash(self) -> None:
        """В спеке описано управление подпиской, но не тело события."""
        adapter = _adapter(_routes())
        assert adapter.parse_webhook({"key": "order.state.inway"}) == []
