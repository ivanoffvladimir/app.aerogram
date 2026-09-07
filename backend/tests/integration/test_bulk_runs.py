"""Массовые отправления: список получателей проходит Decision Engine (ADR-0022).

Проверяется то, ради чего модуль существует: список считается и оформляется
целиком, строка с ошибкой гасит себя, а не прогон, и повторный запуск
не создаёт вторых заказов.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from aerogram.carriers import registry
from tests.integration.conftest import RATE_REQUEST, FakeCarrier, login
from tests.integration.test_shipments_api import ShippingCarrier

pytestmark = pytest.mark.asyncio


def _register(adapter: object) -> None:
    """Реестр намеренно запрещает переопределять уже зарегистрированный код."""
    try:
        registry.get_adapter("fake")
    except LookupError:
        registry.register(adapter)  # type: ignore[arg-type]


def _register_fake() -> None:
    """Перевозчик, который умеет только считать."""
    _register(FakeCarrier("fake"))


def _register_shipping() -> None:
    """Перевозчик, который умеет ещё и оформлять заказы."""
    _register(ShippingCarrier("fake"))


def _payload(rows: int = 2, **overrides: Any) -> dict[str, Any]:
    row = {
        "destination": RATE_REQUEST["destination"],
        "packages": RATE_REQUEST["packages"],
        "cargo_value": RATE_REQUEST["cargo_value"],
        "cargo_type": RATE_REQUEST["cargo_type"],
    }
    return {
        "origin": RATE_REQUEST["origin"],
        "strategy": "optimal",
        "rows": [dict(row) for _ in range(rows)],
        **overrides,
    }


async def _create(client: AsyncClient, headers: dict[str, str], **kw: Any) -> dict[str, Any]:
    response = await client.post("/v1/bulk-runs", json=_payload(**kw), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


class LabelBulkCarrier(ShippingCarrier):
    """Перевозчик прогона, умеющий отдавать печатную форму."""

    def __init__(self) -> None:
        super().__init__("fake")
        self.label_calls: list[str] = []

    async def label(self, ext_id: str, fmt: Any, acc: Any) -> Any:
        from aerogram.carriers.base import LabelResult

        self.label_calls.append(ext_id)
        return LabelResult(format=fmt, content=_one_page_pdf(), is_pending=False)


def _one_page_pdf() -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def label_carrier(carrier_setup: tuple[UUID, UUID]) -> LabelBulkCarrier:
    adapter = LabelBulkCarrier()
    registry.register(adapter)
    return adapter


@pytest.fixture
def label_store(
    monkeypatch: pytest.MonkeyPatch, label_carrier: LabelBulkCarrier
) -> dict[str, bytes]:
    """Хранилище в памяти вместо S3: настоящего здесь нет и не нужно."""
    from aerogram.documents.storage import ObjectStorage

    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(
        ObjectStorage, "_put", staticmethod(lambda key, body, ct: objects.__setitem__(key, body))
    )
    monkeypatch.setattr(ObjectStorage, "_get", staticmethod(lambda key: objects[key]))
    return objects


async def _completed_run(client: AsyncClient, headers: dict[str, str]) -> str:
    """Прогон, доведённый до оформленных отправлений."""
    run_id = (await _create(client, headers))["id"]
    await client.post(f"/v1/bulk-runs/{run_id}/quote", headers=headers)
    await client.post(f"/v1/bulk-runs/{run_id}/select", headers=headers)
    created = await client.post(f"/v1/bulk-runs/{run_id}/create", headers=headers)
    assert created.status_code == 200, created.text
    assert {row["status"] for row in created.json()["rows"]} == {"created"}
    return str(run_id)


class TestDraft:
    async def test_a_run_starts_as_a_draft_named_after_the_date(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        run = await _create(client, headers)
        assert run["status"] == "draft"
        assert run["name"].startswith("Массовый расчёт от ")
        assert [row["position"] for row in run["rows"]] == [1, 2]
        assert {row["status"] for row in run["rows"]} == {"new"}

    async def test_the_name_is_editable(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        run = await _create(client, headers)
        response = await client.patch(
            f"/v1/bulk-runs/{run['id']}", json={"name": "Ноябрьская рассылка"}, headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Ноябрьская рассылка"

    async def test_a_run_needs_at_least_one_recipient(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await client.post("/v1/bulk-runs", json=_payload(rows=0), headers=headers)
        assert response.status_code == 422

    async def test_someone_elses_run_is_not_found_rather_than_forbidden(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Чужой объект по прямому идентификатору — 404, а не 403."""
        run = await _create(client, headers)
        other = await login(client, "b@example.com")
        response = await client.get(f"/v1/bulk-runs/{run['id']}", headers=other)
        assert response.status_code == 404


class TestAutoSelectStaysOut:
    """Прогон не отдаёт выбор правилу автовыбора (ADR-0029).

    Две одинаковые строки списка делят один расчёт (FR-1.6), а ключ решения
    автовыбора выведен из расчёта. Одно решение на две строки означало бы
    одно отправление на две: «одно решение — одно отправление» держит домен,
    и вторая строка просто не оформилась бы.
    """

    async def test_identical_rows_still_get_a_decision_each(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        _register_shipping()
        await client.post(
            "/v1/routing-rules",
            json={
                "name": "берём дешёвое",
                "priority": 10,
                "conditions": {"cargo_type": ["equipment"]},
                "actions": {"auto_select": "cheapest"},
            },
            headers=headers,
        )
        run_id = (await _create(client, headers))["id"]
        await client.post(f"/v1/bulk-runs/{run_id}/quote", headers=headers)

        selected = (await client.post(f"/v1/bulk-runs/{run_id}/select", headers=headers)).json()
        decisions = [row["decision_id"] for row in selected["rows"]]
        assert all(decisions), selected
        assert len(set(decisions)) == 2, "у каждой строки своё решение"

        created = (await client.post(f"/v1/bulk-runs/{run_id}/create", headers=headers)).json()
        assert {row["status"] for row in created["rows"]} == {"created"}


class TestBatchPrinting:
    """Сто этикеток одной кнопкой (ADR-0016).

    Склеенный файл не хранится: он производный, а вторая копия означала бы
    вторую копию персональных данных получателей в хранилище.
    """

    async def test_the_whole_run_prints_as_one_pdf(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        label_store: dict[str, bytes],
    ) -> None:
        run_id = await _completed_run(client, headers)

        ordered = await client.post(f"/v1/bulk-runs/{run_id}/labels", headers=headers)
        assert ordered.status_code == 200, ordered.text
        assert ordered.json() == {"ready": 2, "pending": 0, "failed": 0}

        pack = await client.get(f"/v1/bulk-runs/{run_id}/labels", headers=headers)
        assert pack.status_code == 200, pack.text
        assert pack.content.startswith(b"%PDF")
        # Две этикетки по странице: кладовщик видит, что уйдёт на принтер.
        assert pack.headers["X-Aerogram-Pages"] == "2"
        assert pack.headers["content-disposition"].startswith("attachment")

    async def test_the_pack_is_not_stored(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        label_store: dict[str, bytes],
    ) -> None:
        """В хранилище лежат только отдельные этикетки — по одной на строку."""
        run_id = await _completed_run(client, headers)
        await client.post(f"/v1/bulk-runs/{run_id}/labels", headers=headers)
        await client.get(f"/v1/bulk-runs/{run_id}/labels", headers=headers)

        assert len(label_store) == 2

    async def test_ordering_twice_does_not_call_the_carrier_again(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        label_store: dict[str, bytes],
        label_carrier: LabelBulkCarrier,
    ) -> None:
        """У Почты России вызов тратит суточную квоту неизвестной величины."""
        run_id = await _completed_run(client, headers)
        await client.post(f"/v1/bulk-runs/{run_id}/labels", headers=headers)
        await client.post(f"/v1/bulk-runs/{run_id}/labels", headers=headers)

        assert len(label_carrier.label_calls) == 2, "по вызову на отправление, не больше"

    async def test_without_a_single_ready_label_the_answer_is_a_refusal(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        label_store: dict[str, bytes],
    ) -> None:
        """Пустой PDF на принтере хуже честного отказа: кладовщик решит,
        что печатать нечего."""
        run_id = await _completed_run(client, headers)

        pack = await client.get(f"/v1/bulk-runs/{run_id}/labels", headers=headers)
        assert pack.status_code == 409, pack.text

    async def test_a_foreign_run_is_a_404(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        label_store: dict[str, bytes],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        run_id = await _completed_run(client, headers)
        other = await login(client, "b@example.com")

        assert (
            await client.post(f"/v1/bulk-runs/{run_id}/labels", headers=other)
        ).status_code == 404


class TestRun:
    async def test_the_whole_list_goes_through_quote_select_and_create(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        _register_shipping()
        run = await _create(client, headers)
        run_id = run["id"]

        quoted = (await client.post(f"/v1/bulk-runs/{run_id}/quote", headers=headers)).json()
        assert {row["status"] for row in quoted["rows"]} == {"quoted"}
        assert all(row["rate_quote_id"] for row in quoted["rows"])

        selected = (await client.post(f"/v1/bulk-runs/{run_id}/select", headers=headers)).json()
        assert {row["status"] for row in selected["rows"]} == {"selected"}
        assert all(row["decision_id"] for row in selected["rows"])

        created = (await client.post(f"/v1/bulk-runs/{run_id}/create", headers=headers)).json()
        assert created["status"] == "completed"
        assert {row["status"] for row in created["rows"]} == {"created"}
        assert all(row["shipment_id"] for row in created["rows"])

    async def test_creating_twice_does_not_create_second_orders(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Ключ идемпотентности строки выводится из прогона и строки."""
        _register_shipping()
        run_id = (await _create(client, headers))["id"]
        await client.post(f"/v1/bulk-runs/{run_id}/quote", headers=headers)
        await client.post(f"/v1/bulk-runs/{run_id}/select", headers=headers)
        first = (await client.post(f"/v1/bulk-runs/{run_id}/create", headers=headers)).json()
        second = (await client.post(f"/v1/bulk-runs/{run_id}/create", headers=headers)).json()

        assert [r["shipment_id"] for r in first["rows"]] == [
            r["shipment_id"] for r in second["rows"]
        ]

    async def test_the_counts_show_partial_success_without_walking_the_rows(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        _register_fake()
        run_id = (await _create(client, headers, rows=3))["id"]
        quoted = (await client.post(f"/v1/bulk-runs/{run_id}/quote", headers=headers)).json()
        assert quoted["counts"] == {"quoted": 3}

    async def test_a_row_with_an_impossible_deadline_fails_alone(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Строка, под которую не подошло ни одно предложение, гасит себя.

        Это законный исход расчёта, а не сбой: срок задан такой, что в него
        не укладывается никто. Остальные строки прогона от этого не страдают.
        """
        _register_shipping()
        payload = _payload(rows=2)
        payload["rows"][1]["deadline"] = "2020-01-01T00:00:00+03:00"
        created = await client.post("/v1/bulk-runs", json=payload, headers=headers)
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]

        await client.post(f"/v1/bulk-runs/{run_id}/quote", headers=headers)
        selected = (await client.post(f"/v1/bulk-runs/{run_id}/select", headers=headers)).json()

        statuses = [row["status"] for row in selected["rows"]]
        assert statuses.count("selected") == 1
        failed = next(row for row in selected["rows"] if row["status"] == "failed")
        # Причина обязательна: «что-то произошло, но что — неизвестно»
        # запрещено и схемой, и здравым смыслом.
        assert failed["error_message"]

    async def test_a_broken_adapter_fails_one_row_not_the_whole_run(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Перевозчик, не умеющий оформлять, — отказ строки, а не крах прогона.

        Прогон обрабатывает сотни строк, и неожиданная ошибка на одной
        не должна уносить работу по остальным.
        """
        _register_fake()  # FakeCarrier умеет только считать
        run_id = (await _create(client, headers))["id"]
        await client.post(f"/v1/bulk-runs/{run_id}/quote", headers=headers)
        await client.post(f"/v1/bulk-runs/{run_id}/select", headers=headers)
        response = await client.post(f"/v1/bulk-runs/{run_id}/create", headers=headers)

        assert response.status_code == 200, response.text
        created = response.json()
        assert created["status"] == "failed"
        assert {row["status"] for row in created["rows"]} == {"failed"}
        assert all(row["error_message"] for row in created["rows"])


class TestListing:
    async def test_runs_are_listed_newest_first_without_rows(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        await _create(client, headers)
        await _create(client, headers)
        response = await client.get("/v1/bulk-runs", headers=headers)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 2
        # Список не тащит строки: прогон может быть на тысячу получателей.
        assert page["items"][0]["rows"] == []
        assert page["items"][0]["counts"] == {"new": 2}


ROSPLOMBA = {
    "type": "legal",
    "name": 'ООО "Роспломба"',
    "inn": "7701234567",
    "kpp": "770101001",
    "addresses": [
        {"city": "Москва", "street": "ул Тверская", "house": "1"},
    ],
}


async def _counterparty(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    response = await client.post(
        "/v1/counterparties", json={**ROSPLOMBA, **overrides}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _import(client: AsyncClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    response = await client.post("/v1/bulk-runs/import", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


class TestImport:
    """Стадия 2 (ADR-0022): импорт списка и подбор по адресной книге."""

    async def test_a_plain_list_is_parsed_without_the_address_book(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        result = await _import(client, headers, "Москва; ул. Ленина, 1\nТверь; пр. Мира, 3")
        assert result["counts"] == {"parsed": 2, "resolved": 0, "ambiguous": 0, "not_found": 0}
        assert result["rows"][0]["destination"] == {
            "country": "RU",
            "region": None,
            "city": "Москва",
            "postal_code": None,
            "address_line": "ул. Ленина, 1",
        }
        assert result["errors"] == []

    async def test_a_search_file_finds_the_counterparty_by_inn(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        # «Файл поиска», как у catapulto: строка с ИНН подбирается по
        # собственной адресной книге тенанта.
        created = await _counterparty(client, headers)
        result = await _import(client, headers, "ИНН;Вес\n7701234567;1,5")
        row = result["rows"][0]
        assert row["status"] == "resolved"
        assert row["lookup"] == "ИНН 7701234567"
        assert row["match"]["counterparty_id"] == created["id"]
        assert row["match"]["address_id"] == created["addresses"][0]["id"]
        assert row["destination"]["city"] == "Москва"
        assert row["destination"]["address_line"] == "ул Тверская, 1"
        # Килограммы из файла — в целые граммы, вверх.
        assert row["weight_grams"] == 1500

    async def test_the_name_is_matched_exactly_and_case_insensitively(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        await _counterparty(client, headers)
        result = await _import(client, headers, 'Контрагент\nооо "роспломба"')
        assert result["rows"][0]["status"] == "resolved"

    async def test_a_substring_is_not_a_match(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        # «Роспломба» не должна подбирать «Роспломба-Юг»: список называет
        # получателя целиком.
        await _counterparty(client, headers)
        result = await _import(client, headers, "Контрагент\nРоспломба")
        assert result["rows"][0]["status"] == "not_found"

    async def test_two_addresses_make_the_row_ambiguous(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        await _counterparty(
            client,
            headers,
            addresses=[
                {"city": "Москва", "street": "ул Тверская", "house": "1"},
                {"city": "Тверь", "street": "пр Мира", "house": "3"},
            ],
        )
        result = await _import(client, headers, "ИНН\n7701234567")
        row = result["rows"][0]
        assert row["status"] == "ambiguous"
        assert len(row["match"]["options"]) == 2
        assert row["destination"] is None

    async def test_the_city_in_the_row_narrows_the_choice(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        await _counterparty(
            client,
            headers,
            addresses=[
                {"city": "Москва", "street": "ул Тверская", "house": "1"},
                {"city": "Тверь", "street": "пр Мира", "house": "3"},
            ],
        )
        result = await _import(client, headers, "ИНН;Город\n7701234567;тверь")
        row = result["rows"][0]
        assert row["status"] == "resolved"
        assert row["destination"]["city"] == "Тверь"

    async def test_branches_sharing_an_inn_are_ambiguous(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        await _counterparty(client, headers)
        await _counterparty(client, headers, kpp="770102002", name='ООО "Роспломба", филиал')
        result = await _import(client, headers, "ИНН\n7701234567")
        row = result["rows"][0]
        assert row["status"] == "ambiguous"
        assert "2 контрагента" in row["message"]

    async def test_an_address_without_a_house_is_not_usable(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        # До двери без дома не доехать, а расчёт требует строку адреса.
        await _counterparty(client, headers, addresses=[{"city": "Москва"}])
        result = await _import(client, headers, "ИНН\n7701234567")
        row = result["rows"][0]
        assert row["status"] == "not_found"
        assert "улица и дом" in row["message"]

    async def test_the_address_book_of_another_tenant_is_invisible(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        await _counterparty(client, headers)
        other = await login(client, "b@example.com")
        result = await _import(client, other, "ИНН\n7701234567")
        assert result["rows"][0]["status"] == "not_found"

    async def test_unreadable_lines_are_named_not_dropped(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        result = await _import(client, headers, "Москва; ул. Ленина, 1\nВладивосток")
        assert result["errors"] == ["Строка 2: нужны город и адрес через «;»"]
        assert result["counts"]["parsed"] == 1

    async def test_import_is_a_preview_and_creates_nothing(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        before = (await client.get("/v1/bulk-runs", headers=headers)).json()["total"]
        await _import(client, headers, "Москва; ул. Ленина, 1")
        after = (await client.get("/v1/bulk-runs", headers=headers)).json()["total"]
        assert after == before
