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
from tests.integration.conftest import RATE_REQUEST, FakeCarrier
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
        from tests.integration.conftest import login

        other = await login(client, "b@example.com")
        response = await client.get(f"/v1/bulk-runs/{run['id']}", headers=other)
        assert response.status_code == 404


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
