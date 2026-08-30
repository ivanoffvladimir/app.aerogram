"""Единая лента трекинга (FR-3.2 … FR-3.4).

Проверяется то, что ломается незаметно: порядок событий, дубли из двух
источников, откат статуса назад догоняющим старым событием и факт доставки,
на котором потом строится вся аналитика.

Перевозчик здесь имеет код ``cdek`` намеренно: карта статусов существует
только для настоящих ТК, и тест заодно проверяет её, а не выдуманную.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.carriers.base import (
    Capabilities,
    CarrierAccount,
    RawEvent,
    ShipmentRequest,
    ShipmentResult,
)
from aerogram.config import get_settings
from aerogram.core.models import CarrierAccount as CarrierAccountModel
from aerogram.db import session_scope
from aerogram.directories.models import Carrier, City, CityCarrierMap
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.enums import EventSource
from aerogram.shared.ids import uuid7
from aerogram.shipments.repository import ShipmentRepository
from aerogram.shipments.service import ShipmentService
from aerogram.tracking.models import DeliveryOutcome
from aerogram.tracking.service import TrackingService
from tests.integration.conftest import (
    MOSCOW,
    RATE_REQUEST,
    TEST_KEY,
    VLADIVOSTOK,
    FakeCarrier,
)

pytestmark = pytest.mark.asyncio

#: Дедлайн эталонного запроса. Задаётся явно: без него нечем проверить,
#: считается ли соблюдение срока.
DEADLINE = datetime(2026, 9, 5, 23, 59, tzinfo=UTC)
RATE_REQUEST_WITH_DEADLINE = {**RATE_REQUEST, "deadline": DEADLINE.isoformat()}


class TrackingCarrier(FakeCarrier):
    """Перевозчик, умеющий отдавать историю событий."""

    capabilities = Capabilities(supports_cancel=True)

    def __init__(self, code: str = "cdek") -> None:
        super().__init__(code)
        self.events: list[RawEvent] = []

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        return ShipmentResult(
            external_id=f"EXT-{req.number}",
            tracking_number=f"TRK-{req.number}",
            promised_delivery_date=None,
            price_actual=None,
        )

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        return None

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        return list(self.events)


def event(status_raw: str, *, at: datetime, city: str | None = None) -> RawEvent:
    return RawEvent(occurred_at=at, status_raw=status_raw, city=city)


@pytest.fixture
async def cdek_setup(seeded_tenants: tuple[UUID, UUID], database_url: str) -> UUID:
    """Перевозчик с настоящим кодом, города, сопоставление и учётная запись."""
    tenant_a, _ = seeded_tenants
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    carrier_id, account_id = uuid7(), uuid7()

    cipher = CredentialCipher({"k1": TEST_KEY.split(":", 1)[1]}, "k1")
    encrypted = cipher.encrypt(
        json.dumps({"client_id": "i", "client_secret": "s"}), aad=str(account_id).encode()
    )

    async with factory() as db, db.begin():
        db.add(Carrier(id=carrier_id, code="cdek", name="СДЭК"))
        db.add_all(
            [
                City(id=uuid7(), fias_id=MOSCOW, name="Москва", fias_level=1),
                City(id=uuid7(), fias_id=VLADIVOSTOK, name="Владивосток", fias_level=4),
            ]
        )
        db.add_all(
            [
                CityCarrierMap(
                    id=uuid7(),
                    carrier_id=carrier_id,
                    city_fias_id=fias,
                    carrier_city_code=code,
                    match_method="fias",
                    is_confirmed=True,
                )
                for fias, code in ((MOSCOW, "44"), (VLADIVOSTOK, "75"))
            ]
        )
        await db.flush()
        await db.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)})
        db.add(
            CarrierAccountModel(
                id=account_id,
                tenant_id=tenant_a,
                carrier_id=carrier_id,
                mode="own_contract",
                credentials_encrypted=encrypted,
                is_active=True,
            )
        )
    await engine.dispose()
    return tenant_a


async def _shipment(client: AsyncClient, headers: dict[str, str], key: str = "t-1") -> dict:
    """Пройти расчёт → рекомендацию → решение → создание и вернуть отправление."""
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
    return created.json()


async def _poll(tenant_id: UUID, shipment_id: UUID) -> int:
    """Опросить перевозчика так же, как это сделает фоновая задача."""
    async with session_scope(tenant_id) as session:
        shipment = await ShipmentRepository(session).get(shipment_id)
        assert shipment is not None
        return await ShipmentService(session, get_settings()).poll(shipment)


async def _ingest(tenant_id: UUID, shipment_id: UUID, events: list[RawEvent]) -> int:
    """Принять события так, как их принёс бы вебхук."""
    async with session_scope(tenant_id) as session:
        shipment = await ShipmentRepository(session).get(shipment_id)
        assert shipment is not None
        return await TrackingService(session).ingest(
            shipment, events, carrier_code="cdek", source=EventSource.WEBHOOK
        )


async def _stored(tenant_id: UUID, shipment_id: UUID) -> tuple[Any, Any]:
    """Строка отправления и факт доставки — то, что видит аналитика."""
    async with session_scope(tenant_id) as session:
        shipment = await ShipmentRepository(session).get(shipment_id)
        outcome = (
            await session.execute(
                select(DeliveryOutcome).where(DeliveryOutcome.shipment_id == shipment_id)
            )
        ).scalar_one_or_none()
        return shipment, outcome


@pytest.fixture
def carrier(cdek_setup: UUID) -> TrackingCarrier:
    """Подменить адаптер СДЭК поддельным, оставив его код.

    Код настоящий намеренно: карта статусов существует только для настоящих
    перевозчиков, и тест проверяет её, а не выдуманную. Настоящий адаптер
    регистрируется при сборке приложения, поэтому реестр сначала очищается.
    """
    registry._reset_for_tests()
    adapter = TrackingCarrier()
    registry.register(adapter)
    return adapter


class TestTimeline:
    async def test_events_are_ordered_by_when_they_happened(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Перевозчики отдают события не по порядку.

        Лента, отсортированная по времени получения, показала бы доставку
        раньше отправки.
        """
        shipment = await _shipment(client, headers)
        carrier.events = [
            event("DELIVERED", at=datetime(2026, 9, 4, 15, 0, tzinfo=UTC)),
            event("CREATED", at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC)),
            event("SENT_TO_RECIPIENT_CITY", at=datetime(2026, 9, 2, 20, 0, tzinfo=UTC)),
        ]
        await _poll(cdek_setup, UUID(shipment["id"]))

        response = await client.get(f"/v1/shipments/{shipment['id']}/tracking", headers=headers)

        assert response.status_code == 200, response.text
        assert [e["normalized_status"] for e in response.json()] == [
            "CREATED",
            "IN_TRANSIT",
            "DELIVERED",
        ]

    async def test_the_raw_carrier_status_is_kept_beside_ours(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Разговаривать с перевозчиком оператор будет на языке перевозчика."""
        shipment = await _shipment(client, headers)
        carrier.events = [event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC))]
        await _poll(cdek_setup, UUID(shipment["id"]))

        body = (
            await client.get(f"/v1/shipments/{shipment['id']}/tracking", headers=headers)
        ).json()

        assert body[0]["normalized_status"] == "OUT_FOR_DELIVERY"
        assert body[0]["carrier_status"] == "TAKEN_BY_COURIER"

    async def test_a_foreign_shipment_is_not_an_empty_timeline(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Пустая лента и чужое отправление — разные ответы, 200 и 404."""
        from tests.conftest import login

        shipment = await _shipment(client, headers)
        other = await login(client, "b@example.com")

        response = await client.get(f"/v1/shipments/{shipment['id']}/tracking", headers=other)

        assert response.status_code == 404


class TestDeduplication:
    async def test_the_same_event_from_two_sources_is_stored_once(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Одно событие приходит и вебхуком, и опросом."""
        shipment = await _shipment(client, headers)
        events = [event("CREATED", at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC), city="Москва")]
        carrier.events = events

        assert await _ingest(cdek_setup, UUID(shipment["id"]), events) == 1
        assert await _poll(cdek_setup, UUID(shipment["id"])) == 0

        body = (
            await client.get(f"/v1/shipments/{shipment['id']}/tracking", headers=headers)
        ).json()
        assert len(body) == 1

    async def test_a_batch_containing_its_own_duplicate_survives(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Иначе уникальный индекс откатил бы всю пачку из-за одного повтора."""
        shipment = await _shipment(client, headers)
        same = event("CREATED", at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC))
        carrier.events = [same, same]

        assert await _poll(cdek_setup, UUID(shipment["id"])) == 1


class TestProjection:
    async def test_the_shipment_follows_the_latest_event(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        shipment = await _shipment(client, headers)
        carrier.events = [
            event("CREATED", at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC)),
            event("TAKEN_BY_COURIER", at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC)),
        ]
        await _poll(cdek_setup, UUID(shipment["id"]))

        body = (await client.get(f"/v1/shipments/{shipment['id']}", headers=headers)).json()
        assert body["status"] == "OutForDelivery"

    async def test_a_late_arriving_old_event_does_not_roll_the_status_back(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Догоняющее старое событие — обычное дело, а откат статуса — нет."""
        shipment = await _shipment(client, headers)
        carrier.events = [event("DELIVERED", at=datetime(2026, 9, 4, 15, 0, tzinfo=UTC))]
        await _poll(cdek_setup, UUID(shipment["id"]))

        await _ingest(
            cdek_setup,
            UUID(shipment["id"]),
            [event("SENT_TO_RECIPIENT_CITY", at=datetime(2026, 9, 2, 20, 0, tzinfo=UTC))],
        )

        body = (await client.get(f"/v1/shipments/{shipment['id']}", headers=headers)).json()
        assert body["status"] == "Delivered"

    async def test_polling_stops_after_a_final_status(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        shipment = await _shipment(client, headers)
        carrier.events = [event("DELIVERED", at=datetime(2026, 9, 4, 15, 0, tzinfo=UTC))]
        await _poll(cdek_setup, UUID(shipment["id"]))

        stored, _ = await _stored(cdek_setup, UUID(shipment["id"]))
        assert stored.next_poll_at is None

    async def test_an_unmapped_status_is_flagged_not_fatal(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Новый статус перевозчика не должен ронять приём событий."""
        shipment = await _shipment(client, headers)
        carrier.events = [
            event("СОВЕРШЕННО_НОВЫЙ_СТАТУС", at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC))
        ]

        assert await _poll(cdek_setup, UUID(shipment["id"])) == 1

        body = (
            await client.get(f"/v1/shipments/{shipment['id']}/tracking", headers=headers)
        ).json()
        assert body[0]["normalized_status"] == "IN_TRANSIT"
        assert body[0]["carrier_status"] == "СОВЕРШЕННО_НОВЫЙ_СТАТУС"


class TestDeliveryOutcome:
    async def test_delivery_within_the_deadline_is_recorded_as_met(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Факт доставки — вход обучающего датасета, ради которого всё и строится."""
        shipment = await _shipment(client, headers)
        delivered_at = DEADLINE - timedelta(hours=6)
        carrier.events = [
            event("READY_FOR_SHIPMENT_IN_SENDER_CITY", at=DEADLINE - timedelta(days=3)),
            event("DELIVERED", at=delivered_at),
        ]
        await _poll(cdek_setup, UUID(shipment["id"]))

        stored, outcome = await _stored(cdek_setup, UUID(shipment["id"]))
        assert outcome is not None
        assert outcome.delivered_at == delivered_at
        assert outcome.deadline_met is True
        assert outcome.delay_seconds == 0
        assert stored.is_late is False
        assert stored.actual_delivery_date == delivered_at.date()

    async def test_a_late_delivery_records_how_late(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        shipment = await _shipment(client, headers)
        delivered_at = DEADLINE + timedelta(days=2, hours=1)
        carrier.events = [event("DELIVERED", at=delivered_at)]
        await _poll(cdek_setup, UUID(shipment["id"]))

        stored, outcome = await _stored(cdek_setup, UUID(shipment["id"]))
        assert outcome.deadline_met is False
        assert outcome.delay_seconds == int(timedelta(days=2, hours=1).total_seconds())
        assert stored.is_late is True
        assert stored.delay_days == 2

    async def test_the_outcome_is_written_once_not_twice(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Две строки об одной доставке — два противоречащих факта."""
        shipment = await _shipment(client, headers)
        carrier.events = [event("DELIVERED", at=DEADLINE - timedelta(hours=1))]
        await _poll(cdek_setup, UUID(shipment["id"]))
        await _ingest(
            cdek_setup,
            UUID(shipment["id"]),
            [event("ACCEPTED_AT_PICK_UP_POINT", at=DEADLINE - timedelta(hours=3))],
        )

        _, outcome = await _stored(cdek_setup, UUID(shipment["id"]))
        assert outcome is not None
