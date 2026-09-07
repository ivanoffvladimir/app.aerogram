"""Печатные формы: заказ у перевозчика, хранение, отдача (ADR-0016).

ТЗ v3 требует одного — `Documents: label/waybill where available` в карточке
отправления. Здесь проверяется то, что ошибётся молча: повторный заказ
не стоит второго обращения к перевозчику, файл отдаётся под своей сессией
и только своему тенанту, а неготовая форма не выглядит готовой.

Настоящего S3 нет: подменяется синхронная часть хранилища, которая зовёт
`boto3`. Всё остальное — ключ, перевод отказов, порядок состояний —
настоящее.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from aerogram.carriers import registry
from aerogram.carriers.base import CarrierAccount, LabelResult
from aerogram.documents import service as documents_service
from aerogram.documents.storage import ObjectStorage
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierValidationError
from aerogram.shared.ids import uuid7
from tests.conftest import login
from tests.integration.conftest import RATE_REQUEST_WITH_DEADLINE
from tests.integration.test_shipments_api import ShippingCarrier

pytestmark = pytest.mark.asyncio

LABEL = b"%PDF-1.4 fake label"


class LabelCarrier(ShippingCarrier):
    """Перевозчик, умеющий отдавать печатную форму.

    Считает обращения: «второго вызова не было» иначе проверить нечем —
    ответ клиента выглядит одинаково и при одном обращении, и при двух.
    """

    def __init__(self, *, behaviour: str = "ok") -> None:
        super().__init__("fake")
        self.behaviour = behaviour
        self.label_calls: list[tuple[str, LabelFormat]] = []

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        self.label_calls.append((ext_id, fmt))
        if self.behaviour == "pending":
            return LabelResult(format=fmt, content=None, is_pending=True)
        if self.behaviour == "error":
            raise CarrierValidationError("Форма для этого заказа недоступна", carrier_code="fake")
        return LabelResult(format=fmt, content=LABEL, is_pending=False)


class Store:
    """Хранилище в памяти вместо S3."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = self.objects

        def put(key: str, body: bytes, content_type: str) -> None:
            store[key] = body

        def get(key: str) -> bytes:
            return store[key]

        def delete(key: str) -> None:
            store.pop(key, None)

        monkeypatch.setattr(ObjectStorage, "_put", staticmethod(put))
        monkeypatch.setattr(ObjectStorage, "_get", staticmethod(get))
        monkeypatch.setattr(ObjectStorage, "_delete", staticmethod(delete))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    stub = Store()
    stub.install(monkeypatch)
    return stub


@pytest.fixture
def carrier(carrier_setup: tuple[UUID, UUID]) -> LabelCarrier:
    adapter = LabelCarrier()
    registry.register(adapter)
    return adapter


async def _shipment(client: AsyncClient, headers: dict[str, str], key: str = "doc-1") -> dict:
    """Расчёт → рекомендация → решение → отправление."""
    quote = await client.post("/v1/rates", json=RATE_REQUEST_WITH_DEADLINE, headers=headers)
    assert quote.status_code == 200, quote.text

    recommendation = await client.post(
        "/v1/routing/quote",
        json={"quote_id": quote.json()["quote_id"], "strategy": "optimal"},
        headers=headers,
    )
    assert recommendation.status_code == 200, recommendation.text
    picked = recommendation.json()

    decision = await client.post(
        "/v1/decisions",
        json={
            "recommendation_id": picked["id"],
            "selected_offer_id": picked["recommended_offer_id"],
            "mode": "manual",
        },
        headers={**headers, "Idempotency-Key": f"d-{key}"},
    )
    assert decision.status_code == 201, decision.text

    created = await client.post(
        "/v1/shipments",
        json={"decision_id": decision.json()["decision_id"]},
        headers={**headers, "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _order(client: AsyncClient, headers: dict[str, str], shipment_id: str) -> dict[str, Any]:
    response = await client.post(f"/v1/shipments/{shipment_id}/documents", json={}, headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestOrdering:
    async def test_a_ready_label_is_stored_and_served(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: LabelCarrier,
        store: Store,
    ) -> None:
        shipment = await _shipment(client, headers)
        document = await _order(client, headers, shipment["id"])

        assert document["status"] == "ready"
        assert document["size_bytes"] == len(LABEL)
        assert len(store.objects) == 1

        file = await client.get(f"/v1/documents/{document['id']}/content", headers=headers)
        assert file.status_code == 200, file.text
        assert file.content == LABEL
        assert file.headers["content-type"] == "application/pdf"
        # Этикетку печатают, а не читают на экране: вкладка браузера
        # с персональными данными получателя переживает саму задачу.
        assert file.headers["content-disposition"].startswith("attachment")

    async def test_a_repeat_does_not_call_the_carrier_twice(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: LabelCarrier,
        store: Store,
    ) -> None:
        """У Почты России вызов тратит суточную квоту неизвестной величины."""
        shipment = await _shipment(client, headers)
        first = await _order(client, headers, shipment["id"])
        second = await _order(client, headers, shipment["id"])

        assert first["id"] == second["id"]
        assert len(carrier.label_calls) == 1

    async def test_the_document_shows_up_in_the_list(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: LabelCarrier,
        store: Store,
    ) -> None:
        shipment = await _shipment(client, headers)
        await _order(client, headers, shipment["id"])

        listed = await client.get(f"/v1/shipments/{shipment['id']}/documents", headers=headers)
        assert listed.status_code == 200, listed.text
        assert [d["type"] for d in listed.json()] == ["label"]


class TestNotReady:
    async def test_a_pending_label_is_not_served_as_ready(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Перевозчик формирует форму сам; отдать пустоту как файл нельзя."""
        registry.register(LabelCarrier(behaviour="pending"))
        shipment = await _shipment(client, headers)
        document = await _order(client, headers, shipment["id"])

        assert document["status"] == "pending"
        assert store.objects == {}

        file = await client.get(f"/v1/documents/{document['id']}/content", headers=headers)
        assert file.status_code == 409, file.text

    async def test_the_sweep_picks_it_up_when_it_is_ready(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        store: Store,
        database_url: str,
    ) -> None:
        """Подметание по расписанию, а не отложенная задача: состояние
        в таблице переживает перезапуск брокера."""
        adapter = LabelCarrier(behaviour="pending")
        registry.register(adapter)
        shipment = await _shipment(client, headers)
        document = await _order(client, headers, shipment["id"])
        assert document["status"] == "pending"

        adapter.behaviour = "ok"
        pulled = await _sweep(database_url, carrier_setup[0])

        assert pulled == 1
        listed = await client.get(f"/v1/shipments/{shipment['id']}/documents", headers=headers)
        assert listed.json()[0]["status"] == "ready"

    async def test_the_sweep_gives_up_after_a_day(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        store: Store,
        database_url: str,
    ) -> None:
        """Счётчика попыток в схеме нет, и заводить его — менять таблицу.
        Возраст отвечает на тот же вопрос: сутки без формы означают, что
        её не будет, сколько ни спрашивай."""
        registry.register(LabelCarrier(behaviour="pending"))
        shipment = await _shipment(client, headers)
        await _order(client, headers, shipment["id"])

        await _sweep(
            database_url,
            carrier_setup[0],
            later=documents_service.GIVE_UP_AFTER + timedelta(minutes=1),
        )

        listed = await client.get(f"/v1/shipments/{shipment['id']}/documents", headers=headers)
        assert listed.json()[0]["status"] == "failed"
        assert listed.json()[0]["error"]

    async def test_a_carrier_refusal_is_shown_with_its_reason(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        store: Store,
    ) -> None:
        """Отказ перевозчика гасит документ, а не запрос: оператор просил
        этикетку, и `500` вместо причины не сказал бы ему ничего."""
        registry.register(LabelCarrier(behaviour="error"))
        shipment = await _shipment(client, headers)
        document = await _order(client, headers, shipment["id"])

        assert document["status"] == "failed"
        assert document["error"] == "Форма для этого заказа недоступна"


class TestIsolation:
    async def test_a_foreign_document_is_a_404_not_a_403(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: LabelCarrier,
        store: Store,
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """403 подтвердил бы, что такой документ существует (CLAUDE.md §6)."""
        shipment = await _shipment(client, headers)
        document = await _order(client, headers, shipment["id"])

        stranger = await login(client, "b@example.com")
        response = await client.get(f"/v1/documents/{document['id']}/content", headers=stranger)
        assert response.status_code == 404, response.text

    async def test_an_unknown_document_is_a_404(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await client.get(f"/v1/documents/{uuid7()}/content", headers=headers)
        assert response.status_code == 404, response.text

    async def test_documents_of_a_foreign_shipment_are_a_404(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: LabelCarrier,
        store: Store,
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Пустой список был бы неотличим от пустого у своего отправления,
        то есть отвечал бы на вопрос «а есть ли такое отправление»."""
        shipment = await _shipment(client, headers)

        stranger = await login(client, "b@example.com")
        response = await client.get(f"/v1/shipments/{shipment['id']}/documents", headers=stranger)
        assert response.status_code == 404, response.text


async def _sweep(database_url: str, tenant_id: UUID, *, later: timedelta | None = None) -> int:
    """Прогнать подметание печатных форм под тенантом."""
    from aerogram.config import get_settings
    from aerogram.db import session_scope
    from aerogram.documents.service import DocumentService
    from aerogram.shared.clock import utcnow

    async with session_scope(tenant_id) as session:
        service = DocumentService(session, get_settings())
        return await service.fetch_pending(now=utcnow() + (later or timedelta(minutes=5)))
