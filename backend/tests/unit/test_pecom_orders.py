"""Оформление, поиск, трекинг и печатная форма ПЭК.

Тела запросов сверены с девятью официальными примерами `preregistration`;
ответы собраны по формату из документации — см. `tests/fixtures/pecom/README.md`.
Сеть не используется.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount, Party, Place, ShipmentRequest
from aerogram.carriers.pecom.adapter import PecomAdapter
from aerogram.carriers.pecom.client import SANDBOX_BASE_URL, PecomClient
from aerogram.carriers.pecom.orders import (
    LIST_ORDERS_PATH,
    PRINT_PATH,
    STATUS_HISTORY_PATH,
    SUBMIT_PATH,
    create_payload,
    list_orders_payload,
    parse_printable,
    parse_statuses,
)
from aerogram.carriers.status_map import load_status_map
from aerogram.shared.enums import CargoType, LabelFormat, ShipmentStatus
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pecom"


def load(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _account() -> CarrierAccount:
    return CarrierAccount(
        account_id="acc-1",
        carrier_code="pecom",
        mode="own_contract",
        credentials={"login": "user", "api_key": "key"},
    )


def _shipment(**overrides: object) -> ShipmentRequest:
    defaults: dict[str, object] = {
        "number": "AG-2026-000123",
        "service_code": "3",
        "tariff_code": "3",
        "sender": Party(
            city_fias_id=None,
            city_name="Москва",
            carrier_city_code="a678333f-2a2a-11e9-80ce-00155d713b38",
            address="Россия, Москва, Сормовский проезд, 7Ак2",
            name="Заливные луга",
            contact_person="Иван",
            phone="+74956651112",
            inn="7716542310",
        ),
        "recipient": Party(
            city_fias_id=None,
            city_name="Санкт-Петербург",
            carrier_city_code="36cf9b5e-a415-11dc-a911-000a5e19ccb4",
            address="Россия, Санкт-Петербург, Якорная, 2",
            name="Ромашка",
            contact_person="Ирина",
            phone="+79809991199",
            inn="7707083893",
        ),
        "places": (Place(weight_kg=Decimal("46.32"), length_cm=54, width_cm=42, height_cm=16),),
        "declared_value": Money.from_major("15000", "RUB"),
        "cargo_type": CargoType.PARCEL,
        "pickup": False,
        "delivery_to_door": False,
        "extras": {"cargo_description": "Мебель"},
    }
    defaults.update(overrides)
    return ShipmentRequest(**defaults)  # type: ignore[arg-type]


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> PecomAdapter:
    def factory(_: CarrierAccount) -> PecomClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL)
        return PecomClient(login="user", api_key="key", http_client=inner)

    return PecomAdapter(client_factory=factory)


def _routes(**by_path: object) -> Callable[[httpx.Request], httpx.Response]:
    """Маршруты по хвосту пути: базовый URL контура в него уже входит."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, body in by_path.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(
            200, json={"error": {"title": f"нет маршрута {request.url.path}", "message": ""}}
        )

    return handler


class TestCreatePayload:
    def test_our_number_travels_twice_so_ghosts_can_be_found(self) -> None:
        """orderNumber ищется журналом, customerCorrelation — «для синхронизации»."""
        common = create_payload(_shipment(), description="Мебель")["cargos"][0]["common"]  # type: ignore[index]
        assert common["orderNumber"] == "AG-2026-000123"
        assert common["customerCorrelation"] == "AG-2026-000123"

    def test_shape_matches_the_official_example(self) -> None:
        """Сверка с «Preregistration получатель юр.лицо до терминала с ЗТУ»."""
        payload = create_payload(_shipment(), description="Мебель")
        sender = payload["sender"]
        assert sender["warehouseId"] == "a678333f-2a2a-11e9-80ce-00155d713b38"  # type: ignore[index]
        assert sender["legalForm"] == 1  # type: ignore[index]
        assert sender["inn"] == "7716542310"  # type: ignore[index]
        assert sender["personPhones"] == [{"phone": "+74956651112"}]  # type: ignore[index]
        receiver = payload["cargos"][0]["receiver"]  # type: ignore[index]
        assert receiver["warehouseId"] == "36cf9b5e-a415-11dc-a911-000a5e19ccb4"

    def test_self_delivery_and_pickup_are_different_order_types(self) -> None:
        """0 — самопривоз на склад, 3 — забор от отправителя."""
        assert create_payload(_shipment(), description="Груз")["sender"]["orderType"] == 0  # type: ignore[index]
        with_pickup = create_payload(_shipment(pickup=True), description="Груз")
        assert with_pickup["sender"]["orderType"] == 3  # type: ignore[index]
        # «plannedDate обязательный для orderType 3».
        assert "plannedDate" in with_pickup["sender"]  # type: ignore[operator]
        assert "addressStock" in with_pickup["sender"]  # type: ignore[operator]

    def test_an_individual_gets_a_name_block_not_an_inn(self) -> None:
        recipient = Party(
            city_fias_id=None,
            city_name="Россошь",
            carrier_city_code=None,
            address="Россия, г. Россошь, ул. Кленовая, д. 10",
            name="Иванов Николай Викторович",
            phone="+79999991155",
        )
        payload = create_payload(
            _shipment(recipient=recipient, delivery_to_door=True), description="Груз"
        )
        block = payload["cargos"][0]["receiver"]  # type: ignore[index]
        assert block["legalForm"] == 3
        assert block["individual"] == {
            "lastName": "Иванов",
            "firstName": "Николай",
            "patronymic": "Викторович",
        }
        assert "inn" not in block

    def test_insurance_carries_the_declared_value(self) -> None:
        services = create_payload(_shipment(insurance=True), description="Груз")["cargos"][0][  # type: ignore[index]
            "services"
        ]
        assert services["insurance"] == {
            "enabled": True,
            "cost": 15000.0,
            "payer": {"type": 1},
        }

    def test_no_places_is_refused(self) -> None:
        with pytest.raises(ValueError, match="без грузовых мест"):
            create_payload(_shipment(places=()), description="Груз")


class TestStatusParsing:
    def test_timestamps_are_read_as_utc_plus_three(self) -> None:
        """«Все время в часовом поясе UTC + 3 часа», а смещения в строке нет.

        Прочитать метку как UTC значит сдвинуть всю ленту на три часа.
        """
        events = parse_statuses({"items": load("status_history_ok")})
        assert events[0].occurred_at == datetime(2022, 9, 26, 14, 42, 37, tzinfo=UTC)

    def test_cancelled_status_stays_in_the_feed_but_does_not_count(self) -> None:
        """isCancel: «статус был выставлен, а позднее отменён»."""
        events = parse_statuses({"items": load("status_history_ok")})
        cancelled = events[1]
        assert cancelled.status_raw == "ОТМЕНЁННЫЙ СТАТУС"
        assert "отменён" in (cancelled.comment or "")
        # Нормализатор не должен принять его за наступивший.
        status, unmapped = load_status_map("pecom").normalize(cancelled.status_raw)
        assert status is ShipmentStatus.IN_TRANSIT
        assert unmapped is True

    def test_events_are_ordered_and_named(self) -> None:
        events = parse_statuses({"items": load("status_history_ok")})
        assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)
        assert events[-1].status_raw == "Выдан на складе"

    def test_rows_without_time_or_name_are_skipped(self) -> None:
        body = {"items": [{"cargoCode": "1", "statuses": [{"name": "В пути"}, {"date": "x"}]}]}
        assert parse_statuses(body) == []


class TestStatusMap:
    def test_names_from_the_documentation_are_mapped(self) -> None:
        status_map = load_status_map("pecom")
        assert status_map.normalize("В пути") == (ShipmentStatus.IN_TRANSIT, False)
        assert status_map.normalize("Выдан получателю") == (ShipmentStatus.DELIVERED, False)
        assert status_map.normalize("Возвращен отправителю") == (ShipmentStatus.RETURNED, False)

    def test_an_unknown_name_does_not_break_the_feed(self) -> None:
        status, unmapped = load_status_map("pecom").normalize("Новый статус")
        assert status is ShipmentStatus.IN_TRANSIT
        assert unmapped is True


class TestSearchWindow:
    def test_the_window_is_narrow_and_by_submission_date(self) -> None:
        """selectBy 1 — «по дате подачи заявки»: сверка ищет то, что создавали."""
        payload = list_orders_payload(today=date(2026, 9, 4), days=7)
        assert payload == {
            "selectBy": 1,
            "dateBegin": "2026-08-28",
            "dateEnd": "2026-09-04",
        }


class TestPrintable:
    def test_base64_pdf_is_decoded(self) -> None:
        content = parse_printable(load("print_ok"))
        assert content is not None
        assert content.startswith(b"%PDF")

    def test_a_bare_string_is_accepted_too(self) -> None:
        """Формат ответа в документации записан неоднозначно."""
        import base64

        pdf = b"%PDF-1.4 minimal"
        assert parse_printable(base64.b64encode(pdf).decode()) == pdf

    def test_anything_that_is_not_a_pdf_is_refused(self) -> None:
        assert parse_printable({"file": "не base64"}) is None
        assert parse_printable(None) is None


@pytest.mark.anyio
class TestAdapter:
    async def test_created_cargo_is_not_pending(self) -> None:
        """ПЭК отдаёт код груза сразу — в отличие от Деловых Линий."""
        adapter = _adapter(_routes(**{SUBMIT_PATH: load("submit_ok")}))
        result = await adapter.create(_shipment(), _account())
        assert result.external_id == "999940950644"
        assert result.is_pending is False
        # Ключ штрих-кода в документации напечатан кириллической «с».
        assert result.tracking_number == "999940950644"

    async def test_create_without_a_description_is_refused(self) -> None:
        adapter = _adapter(_routes(**{SUBMIT_PATH: load("submit_ok")}))
        with pytest.raises(CarrierValidationError, match="наименование груза"):
            await adapter.create(_shipment(extras={}), _account())

    async def test_a_logical_error_with_status_200_is_still_an_error(self) -> None:
        adapter = _adapter(_routes(**{SUBMIT_PATH: load("error_logical")}))
        with pytest.raises(CarrierError, match="обязательный параметр"):
            await adapter.create(_shipment(), _account())

    async def test_find_by_our_number_reads_both_alphabets(self) -> None:
        """В документации ключи журнала напечатаны кириллической «с»."""
        adapter = _adapter(_routes(**{LIST_ORDERS_PATH: load("list_orders_found")}))
        found = await adapter.find_by_number("AG-2026-000123", _account())
        assert found is not None
        assert found.external_id == "999940950644"

    async def test_a_foreign_order_number_is_not_ours(self) -> None:
        adapter = _adapter(_routes(**{LIST_ORDERS_PATH: load("list_orders_found")}))
        assert await adapter.find_by_number("AG-2026-000404", _account()) is None

    async def test_empty_journal_means_creation_really_failed(self) -> None:
        adapter = _adapter(_routes(**{LIST_ORDERS_PATH: load("list_orders_empty")}))
        assert await adapter.find_by_number("AG-2026-000123", _account()) is None

    async def test_track_returns_the_feed(self) -> None:
        adapter = _adapter(_routes(**{STATUS_HISTORY_PATH: load("status_history_ok")}))
        events = await adapter.track("999940950644", _account())
        assert len(events) == 3

    async def test_label_returns_a_pdf(self) -> None:
        adapter = _adapter(_routes(**{PRINT_PATH: load("print_ok")}))
        label = await adapter.label("999940950644", LabelFormat.PDF_A4, _account())
        assert label.is_pending is False
        assert label.content is not None
        assert label.content.startswith(b"%PDF")

    async def test_an_unreadable_form_waits_instead_of_returning_rubbish(self) -> None:
        adapter = _adapter(_routes(**{PRINT_PATH: {"file": "не base64"}}))
        label = await adapter.label("999940950644", LabelFormat.PDF_A4, _account())
        assert label.is_pending is True
        assert label.content is None

    async def test_only_pdf_is_offered(self) -> None:
        adapter = _adapter(_routes(**{PRINT_PATH: load("print_ok")}))
        with pytest.raises(CarrierValidationError, match="только в PDF"):
            await adapter.label("999940950644", LabelFormat.ZPL, _account())
