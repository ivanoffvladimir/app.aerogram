"""Разбор исключений: что попадает на экран оператора и что не попадает.

Главная проверка здесь — сорванный срок у ещё не доставленного отправления.
``shipments.is_late`` проставляется в момент доставки, то есть постфактум,
и до неё платформа о срыве молчала: оператор узнавал о нём тогда, когда
сделать уже ничего нельзя.

Часы разбора подменяются намеренно. Дедлайн эталонного запроса — фиксированная
дата, и на настоящих часах тест «срок ещё не прошёл» позеленел бы сегодня
и покраснел бы в сентябре.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient

from aerogram.carriers.base import RawEvent
from aerogram.config import get_settings
from aerogram.db import session_scope
from aerogram.shared.enums import EventSource
from aerogram.shipments.repository import ShipmentRepository
from aerogram.shipments.service import ShipmentService
from aerogram.tracking.service import TrackingService
from tests.integration.conftest import (
    DEADLINE,
    RATE_REQUEST_WITH_DEADLINE,
    TrackingCarrier,
    event,
)

pytestmark = pytest.mark.asyncio

#: Момент после срока и момент до него.
AFTER_DEADLINE = DEADLINE + timedelta(days=1)
BEFORE_DEADLINE = DEADLINE - timedelta(days=1)

#: Событие настолько старое, что порог тишины адаптивного опроса пройден
#: при любых настоящих часах, которыми считается сам опрос.
LONG_AGO = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Часы разбора стоят после срока: срок сорван."""
    monkeypatch.setattr("aerogram.tracking.exceptions.utcnow", lambda: AFTER_DEADLINE)


async def _shipment(client: AsyncClient, headers: dict[str, str], key: str = "e-1") -> dict:
    """Расчёт → рекомендация → решение → создание."""
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


async def _poll(tenant_id: UUID, shipment_id: UUID) -> None:
    async with session_scope(tenant_id) as session:
        shipment = await ShipmentRepository(session).get(shipment_id)
        assert shipment is not None
        await ShipmentService(session, get_settings()).poll(shipment)


async def _ingest(tenant_id: UUID, shipment_id: UUID, events: list[RawEvent]) -> None:
    async with session_scope(tenant_id) as session:
        shipment = await ShipmentRepository(session).get(shipment_id)
        assert shipment is not None
        await TrackingService(session).ingest(
            shipment, events, carrier_code="cdek", source=EventSource.WEBHOOK
        )


class TestDeadlinePassed:
    async def test_an_undelivered_shipment_past_its_deadline_is_listed(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        now: None,
    ) -> None:
        """До этого срыв срока был виден только после доставки."""
        shipment = await _shipment(client, headers)

        response = await client.get("/v1/tracking/exceptions", headers=headers)

        body = response.json()
        assert response.status_code == 200, response.text
        assert [item["number"] for item in body["items"]] == [shipment["number"]]
        assert body["items"][0]["reasons"] == ["deadline_passed"]
        assert body["by_reason"]["deadline_passed"] == 1

    async def test_before_the_deadline_nothing_is_listed(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Едущее в срок — не исключение, а норма."""
        monkeypatch.setattr("aerogram.tracking.exceptions.utcnow", lambda: BEFORE_DEADLINE)
        await _shipment(client, headers)

        body = (await client.get("/v1/tracking/exceptions", headers=headers)).json()

        assert body["items"] == []
        assert body["total"] == 0
        assert body["scanned"] == 1

    async def test_a_delivered_shipment_leaves_the_list(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        now: None,
    ) -> None:
        """Доставленное с опозданием разбирать поздно — это уже аналитика."""
        shipment = await _shipment(client, headers)
        carrier.events = [event("DELIVERED", at=datetime(2026, 9, 6, 10, 0, tzinfo=UTC))]
        await _poll(cdek_setup, UUID(shipment["id"]))

        body = (await client.get("/v1/tracking/exceptions", headers=headers)).json()

        assert body["items"] == []
        # И само отправление действительно опоздало — иначе тест доказывал бы
        # лишь то, что список пуст по какой-то другой причине.
        card = (await client.get(f"/v1/shipments/{shipment['id']}", headers=headers)).json()
        assert card["status"] == "Delivered"


class TestProblemStatus:
    async def test_a_failed_delivery_attempt_is_listed(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Статус сам по себе означает разбор, даже когда срок ещё не вышел."""
        monkeypatch.setattr("aerogram.tracking.exceptions.utcnow", lambda: BEFORE_DEADLINE)
        shipment = await _shipment(client, headers)
        await _ingest(
            cdek_setup,
            UUID(shipment["id"]),
            [event("NOT_DELIVERED", at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC))],
        )

        body = (await client.get("/v1/tracking/exceptions", headers=headers)).json()

        assert [item["number"] for item in body["items"]] == [shipment["number"]]
        assert body["items"][0]["reasons"] == ["problem_status"]

    async def test_two_reasons_are_both_reported(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        now: None,
    ) -> None:
        """Срыв срока и неудачное вручение разбираются по-разному.

        Схлопнуть их в одну причину значило бы потерять половину работы.
        """
        shipment = await _shipment(client, headers)
        await _ingest(
            cdek_setup,
            UUID(shipment["id"]),
            [event("NOT_DELIVERED", at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC))],
        )

        body = (await client.get("/v1/tracking/exceptions", headers=headers)).json()

        assert body["items"][0]["reasons"] == ["deadline_passed", "problem_status"]
        assert body["by_reason"]["deadline_passed"] == 1
        assert body["by_reason"]["problem_status"] == 1
        # Счётчиков больше, чем строк: это одно отправление и две беды.
        assert body["total"] == 1


class TestStalled:
    async def test_a_carrier_gone_silent_is_listed(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Отсутствие событий само по себе новость (раздел 10 ТЗ).

        Порог тишины берётся из адаптивного опроса и здесь не дублируется:
        два разных порога разошлись бы, и опрос считал бы отправление живым,
        пока экран показывает его зависшим.
        """
        monkeypatch.setattr("aerogram.tracking.exceptions.utcnow", lambda: BEFORE_DEADLINE)
        shipment = await _shipment(client, headers)
        # Событие сильно старше порога: перевозчик молчит месяц.
        await _ingest(
            cdek_setup,
            UUID(shipment["id"]),
            [event("SENT_TO_RECIPIENT_CITY", at=LONG_AGO)],
        )

        body = (await client.get("/v1/tracking/exceptions", headers=headers)).json()

        assert [item["number"] for item in body["items"]] == [shipment["number"]]
        assert body["items"][0]["reasons"] == ["stalled"]
        assert body["items"][0]["last_event_at"].startswith("2026-07")


class TestIsolation:
    async def test_another_tenant_sees_nothing(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        now: None,
    ) -> None:
        """Разбор идёт под RLS, как и всё остальное."""
        from tests.conftest import login

        await _shipment(client, headers)
        other = await login(client, "b@example.com")

        body = (await client.get("/v1/tracking/exceptions", headers=other)).json()

        assert body["items"] == []
        assert body["scanned"] == 0
