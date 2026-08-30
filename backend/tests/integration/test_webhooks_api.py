"""Исходящие вебхуки: подписка, постановка в очередь, доставка (FR-3.6).

Разрешение имён подменяется: тест, зависящий от внешнего DNS, рано или поздно
мигнёт по причине, не имеющей отношения к проверяемому коду. Сама проверка
адреса при этом настоящая — подменяется только источник адресов.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from aerogram.config import get_settings
from aerogram.db import session_scope
from aerogram.shared.enums import EventSource
from aerogram.shipments.repository import ShipmentRepository
from aerogram.tracking import outgoing
from aerogram.tracking.models import WebhookDelivery
from aerogram.tracking.service import TrackingService
from aerogram.tracking.webhooks import MAX_ATTEMPTS, WebhookService
from tests.integration.conftest import DEADLINE, TrackingCarrier, event
from tests.integration.test_tracking_api import _shipment

pytestmark = pytest.mark.asyncio

HOOK_URL = "https://hooks.client.example/aerogram"
ALL_EVENTS = [
    "shipment.status_changed",
    "shipment.delivered",
    "shipment.exception",
    "shipment.delayed",
]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Имя получателя разрешается в публичный адрес.

    Подменяется именно резолвер, а не проверка: логика отказа во внутренние
    сети остаётся той же, что в бою.
    """

    async def fake(host: str) -> list[str]:
        if host == "hooks.client.example":
            return ["93.184.216.34"]
        raise socket.gaierror(f"нет записи для {host}")

    monkeypatch.setattr(outgoing, "resolve", fake)


async def _subscribe(
    client: AsyncClient, headers: dict[str, str], events: list[str] | None = None
) -> dict[str, Any]:
    response = await client.post(
        "/v1/webhooks/subscriptions",
        json={"url": HOOK_URL, "events": events or ALL_EVENTS},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _deliveries(tenant_id: UUID) -> list[WebhookDelivery]:
    async with session_scope(tenant_id) as session:
        rows = await session.execute(select(WebhookDelivery).order_by(WebhookDelivery.created_at))
        return list(rows.scalars())


class TestSubscription:
    async def test_the_secret_is_shown_once_and_never_again(
        self, client: AsyncClient, headers: dict[str, str], cdek_setup: UUID
    ) -> None:
        """Как и API-ключ: у нас он лежит зашифрованным и невосстановим."""
        created = await _subscribe(client, headers)
        assert created["secret"]

        listed = await client.get("/v1/webhooks/subscriptions", headers=headers)
        assert listed.status_code == 200
        assert "secret" not in listed.json()[0]

    async def test_an_unknown_event_is_refused(
        self, client: AsyncClient, headers: dict[str, str], cdek_setup: UUID
    ) -> None:
        """Молча проглотить — значит пообещать доставку, которой не будет."""
        response = await client.post(
            "/v1/webhooks/subscriptions",
            json={"url": HOOK_URL, "events": ["shipment.teleported"]},
            headers=headers,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["field"] == "events"

    async def test_an_internal_address_is_refused(
        self, client: AsyncClient, headers: dict[str, str], cdek_setup: UUID
    ) -> None:
        """Иначе платформа стала бы инструментом разведки чужой сети."""
        response = await client.post(
            "/v1/webhooks/subscriptions",
            json={"url": "https://127.0.0.1/hook", "events": ALL_EVENTS},
            headers=headers,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["field"] == "url"

    async def test_another_tenant_sees_nothing(
        self, client: AsyncClient, headers: dict[str, str], cdek_setup: UUID
    ) -> None:
        from tests.conftest import login

        await _subscribe(client, headers)
        other = await login(client, "b@example.com")

        assert (await client.get("/v1/webhooks/subscriptions", headers=other)).json() == []


class TestEnqueue:
    async def test_a_status_change_queues_a_delivery(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        await _subscribe(client, headers)
        shipment = await _shipment(client, headers)

        async with session_scope(cdek_setup) as session:
            stored = await ShipmentRepository(session).get(UUID(shipment["id"]))
            assert stored is not None
            await TrackingService(session, get_settings()).ingest(
                stored,
                [event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC))],
                carrier_code="cdek",
                source=EventSource.WEBHOOK,
            )

        queued = await _deliveries(cdek_setup)
        assert [d.event_type for d in queued] == ["shipment.status_changed"]

    async def test_an_unchanged_status_queues_nothing(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Уведомление о том, что ничего не изменилось, — шум.

        От такого шума получатель перестаёт читать уведомления вообще.
        """
        await _subscribe(client, headers)
        shipment = await _shipment(client, headers)
        same = [event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC))]

        for _ in range(2):
            async with session_scope(cdek_setup) as session:
                stored = await ShipmentRepository(session).get(UUID(shipment["id"]))
                assert stored is not None
                await TrackingService(session, get_settings()).ingest(
                    stored, same, carrier_code="cdek", source=EventSource.WEBHOOK
                )

        assert len(await _deliveries(cdek_setup)) == 1

    async def test_delivery_queues_both_events(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Доставлено — это и смена статуса, и отдельное событие доставки."""
        await _subscribe(client, headers)
        shipment = await _shipment(client, headers)

        async with session_scope(cdek_setup) as session:
            stored = await ShipmentRepository(session).get(UUID(shipment["id"]))
            assert stored is not None
            await TrackingService(session, get_settings()).ingest(
                stored,
                [event("DELIVERED", at=DEADLINE - timedelta(hours=2))],
                carrier_code="cdek",
                source=EventSource.WEBHOOK,
            )

        assert sorted(d.event_type for d in await _deliveries(cdek_setup)) == [
            "shipment.delivered",
            "shipment.status_changed",
        ]

    async def test_only_subscribed_events_are_queued(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        await _subscribe(client, headers, ["shipment.delivered"])
        shipment = await _shipment(client, headers)

        async with session_scope(cdek_setup) as session:
            stored = await ShipmentRepository(session).get(UUID(shipment["id"]))
            assert stored is not None
            await TrackingService(session, get_settings()).ingest(
                stored,
                [event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC))],
                carrier_code="cdek",
                source=EventSource.WEBHOOK,
            )

        assert await _deliveries(cdek_setup) == []

    async def test_the_payload_carries_no_personal_data(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Адреса, телефоны и имена в исходящее не попадают (CLAUDE.md §6).

        Получатель знает свой заказ по номеру — этого хватает, чтобы найти
        остальное у себя.
        """
        await _subscribe(client, headers)
        shipment = await _shipment(client, headers)

        async with session_scope(cdek_setup) as session:
            stored = await ShipmentRepository(session).get(UUID(shipment["id"]))
            assert stored is not None
            await TrackingService(session, get_settings()).ingest(
                stored,
                [event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC))],
                carrier_code="cdek",
                source=EventSource.WEBHOOK,
            )

        body = str((await _deliveries(cdek_setup))[0].payload)
        assert "Светланская" not in body
        assert "Тверская" not in body
        assert set((await _deliveries(cdek_setup))[0].payload["shipment"]) == {
            "id",
            "number",
            "status",
            "carrier_status",
            "tracking_number",
            "external_id",
        }


class TestDelivery:
    async def _queue(
        self, client: AsyncClient, headers: dict[str, str], tenant_id: UUID, carrier: Any
    ) -> None:
        await _subscribe(client, headers)
        shipment = await _shipment(client, headers)
        async with session_scope(tenant_id) as session:
            stored = await ShipmentRepository(session).get(UUID(shipment["id"]))
            assert stored is not None
            await TrackingService(session, get_settings()).ingest(
                stored,
                [event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC))],
                carrier_code="cdek",
                source=EventSource.WEBHOOK,
            )

    async def test_an_accepted_delivery_is_not_repeated(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._queue(client, headers, cdek_setup, carrier)
        sent: list[str] = []

        async def ok(url: str, secret: str, event_type: str, payload: dict[str, Any]) -> int:
            sent.append(event_type)
            return 204

        monkeypatch.setattr("aerogram.tracking.webhooks.deliver", ok)

        async with session_scope(cdek_setup) as session:
            assert await WebhookService(session, get_settings()).deliver_due() == 1
        async with session_scope(cdek_setup) as session:
            assert await WebhookService(session, get_settings()).deliver_due() == 0

        assert len(sent) == 1, "принятую доставку отправили повторно"

    async def test_a_refused_delivery_is_rescheduled_then_given_up(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Пять попыток, потом снимаем с очереди (FR-3.6)."""
        await self._queue(client, headers, cdek_setup, carrier)

        async def refuse(url: str, secret: str, event_type: str, payload: dict[str, Any]) -> int:
            return 500

        monkeypatch.setattr("aerogram.tracking.webhooks.deliver", refuse)

        for _ in range(MAX_ATTEMPTS):
            async with session_scope(cdek_setup) as session:
                # Срок следующей попытки сдвигается вперёд, поэтому для проверки
                # исчерпания его возвращают в прошлое. Обновление прямое:
                # выборка «пора отправлять» такую доставку уже не вернёт.
                await session.execute(
                    update(WebhookDelivery)
                    .where(
                        WebhookDelivery.delivered_at.is_(None),
                        WebhookDelivery.next_attempt_at.is_not(None),
                    )
                    .values(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
                )
                await WebhookService(session, get_settings()).deliver_due()

        rows = await _deliveries(cdek_setup)
        assert rows[0].attempt == MAX_ATTEMPTS
        assert rows[0].next_attempt_at is None, "продолжаем звонить в закрытую дверь"
        assert rows[0].delivered_at is None

    async def test_a_disabled_subscription_stops_pending_deliveries(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Отписались, пока доставка ждала очереди, — отправлять некуда."""
        created = await _subscribe(client, headers)
        shipment = await _shipment(client, headers)
        async with session_scope(cdek_setup) as session:
            stored = await ShipmentRepository(session).get(UUID(shipment["id"]))
            assert stored is not None
            await TrackingService(session, get_settings()).ingest(
                stored,
                [event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC))],
                carrier_code="cdek",
                source=EventSource.WEBHOOK,
            )

        assert (
            await client.delete(f"/v1/webhooks/subscriptions/{created['id']}", headers=headers)
        ).status_code == 204

        called = False

        async def never(*args: object, **kwargs: object) -> int:
            nonlocal called
            called = True
            return 204

        monkeypatch.setattr("aerogram.tracking.webhooks.deliver", never)
        async with session_scope(cdek_setup) as session:
            await WebhookService(session, get_settings()).deliver_due()

        assert not called
