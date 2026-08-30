"""Carrier Score: пересчёт по домену и выдача аналитики (FR-7).

Главное, что проверяется, — поведение на холодном старте. Раздел 10.2 ТЗ
называет его главным риском функции: показать выдуманную цифру хуже, чем
не показать никакой, потому что один неверный совет в первый месяц дороже,
чем отсутствие функции.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.db import session_scope
from aerogram.directories.models import Carrier
from aerogram.intelligence.score import FORMULA_VERSION
from aerogram.intelligence.service import ScoreService
from aerogram.shared.enums import ShipmentStatus
from aerogram.shared.ids import uuid7
from aerogram.shipments.models import Shipment
from aerogram.tracking.models import DeliveryOutcome, ShipmentEvent

pytestmark = pytest.mark.asyncio

PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 8, 31)
CREATED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def carriers(seeded_tenants: tuple[UUID, UUID], database_url: str) -> tuple[UUID, UUID]:
    """Два перевозчика: надёжный и проблемный."""
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    good_id, bad_id = uuid7(), uuid7()
    async with factory() as db, db.begin():
        db.add_all(
            [
                Carrier(id=good_id, code="good", name="Надёжный"),
                Carrier(id=bad_id, code="bad", name="Проблемный"),
            ]
        )
    await engine.dispose()
    return good_id, bad_id


async def seed_shipments(
    tenant_id: UUID,
    carrier_id: UUID,
    *,
    count: int,
    on_time: int,
    incidents: int = 0,
    cancelled: int = 0,
    cost_minor: int = 100_000,
    events_each: int = 3,
) -> None:
    """Записать завершённые отправления с заданным исходом.

    Через SQL, а не через API: чтобы получить статистически значимую выборку,
    нужны сотни отправлений, и гонять их через расчёт с решением значило бы
    проверять не то, что проверяется.
    """
    async with session_scope(tenant_id) as session:
        for index in range(count):
            # Диапазоны разведены намеренно: отменённые идут первыми, всё
            # остальное — доставленные, и «в срок» считается среди них.
            # Пересекающиеся условия дали бы выборку, которой в жизни не бывает.
            is_cancelled = index < cancelled
            delivered_index = index - cancelled
            shipment = Shipment(
                id=uuid7(),
                tenant_id=tenant_id,
                # Хвост, а не начало: uuid7 упорядочен по времени, и у двух
                # перевозчиков, созданных в одну миллисекунду, начало совпадает.
                number=f"S-{carrier_id.hex[-8:]}-{index}",
                carrier_id=carrier_id,
                status=ShipmentStatus.CANCELLED if is_cancelled else ShipmentStatus.DELIVERED,
                currency="RUB",
                price_quoted_amount_minor=cost_minor,
                has_incident=not is_cancelled and delivered_index < incidents,
                incident_type=(
                    "damage" if not is_cancelled and delivered_index < incidents else None
                ),
                created_at=CREATED_AT,
                updated_at=CREATED_AT,
            )
            session.add(shipment)
            await session.flush()

            if not is_cancelled:
                met = delivered_index < on_time
                session.add(
                    DeliveryOutcome(
                        shipment_id=shipment.id,
                        tenant_id=tenant_id,
                        delivered_at=CREATED_AT + timedelta(days=3),
                        deadline_met=met,
                        delay_seconds=0 if met else 86_400,
                    )
                )
            for event_index in range(events_each):
                session.add(
                    ShipmentEvent(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        shipment_id=shipment.id,
                        occurred_at=CREATED_AT + timedelta(hours=event_index),
                        status_normalized=ShipmentStatus.IN_TRANSIT,
                        status_raw="IN_TRANSIT",
                        source="api_poll",
                        dedup_key=f"{shipment.id}-{event_index}",
                    )
                )


async def recalculate(tenant_id: UUID) -> None:
    async with session_scope(tenant_id) as session:
        await ScoreService(session).recalculate(PERIOD_START, PERIOD_END)


def by_code(rows: list[dict], code: str) -> dict:
    return next(row for row in rows if row["carrier_code"] == code)


class TestColdStart:
    async def test_a_carrier_without_data_says_so_instead_of_showing_a_number(
        self, client: AsyncClient, headers: dict[str, str], carriers: tuple[UUID, UUID]
    ) -> None:
        """Ноль читается как «худший перевозчик», а он всего лишь новый."""
        response = await client.get("/v1/analytics/carriers", headers=headers)

        assert response.status_code == 200, response.text
        rows = response.json()
        assert {r["carrier_code"] for r in rows} >= {"good", "bad"}
        assert by_code(rows, "good")["score"] is None
        assert by_code(rows, "good")["confidence"] == "insufficient"

    async def test_a_carrier_stays_in_the_list_without_a_score(
        self, client: AsyncClient, headers: dict[str, str], carriers: tuple[UUID, UUID]
    ) -> None:
        """Отсутствие в списке оператор прочитал бы как «не подключён»."""
        rows = (await client.get("/v1/analytics/carriers", headers=headers)).json()
        assert by_code(rows, "bad")["carrier_name"] == "Проблемный"

    async def test_nine_shipments_are_still_not_enough(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carriers: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """FR-7.3: граница в десять наблюдений, ниже — числа нет."""
        tenant_a, _ = seeded_tenants
        good, _ = carriers
        await seed_shipments(tenant_a, good, count=9, on_time=9)
        await recalculate(tenant_a)

        rows = (await client.get("/v1/analytics/carriers", headers=headers)).json()
        assert by_code(rows, "good")["score"] is None
        assert by_code(rows, "good")["sample_size"] == 9


class TestScoring:
    async def test_a_reliable_carrier_outscores_a_broken_one(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carriers: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Ради этого различия функция и существует."""
        tenant_a, _ = seeded_tenants
        good, bad = carriers
        await seed_shipments(tenant_a, good, count=200, on_time=196)
        await seed_shipments(tenant_a, bad, count=200, on_time=60, incidents=40, cancelled=30)
        await recalculate(tenant_a)

        rows = (await client.get("/v1/analytics/carriers", headers=headers)).json()
        assert by_code(rows, "good")["score"] > by_code(rows, "bad")["score"]

    async def test_the_breakdown_is_returned_with_the_sample(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carriers: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """FR-7.5: без расшифровки скор — непрозрачное число."""
        tenant_a, _ = seeded_tenants
        good, _ = carriers
        await seed_shipments(tenant_a, good, count=120, on_time=90)
        await recalculate(tenant_a)

        row = by_code((await client.get("/v1/analytics/carriers", headers=headers)).json(), "good")
        assert row["confidence"] == "high"
        assert row["sample_size"] == 120
        assert row["period_start"] == PERIOD_START.isoformat()
        assert row["formula_version"] == FORMULA_VERSION
        assert float(row["components"]["on_time_rate"]) == pytest.approx(0.75, abs=0.001)
        assert row["scope_type"] == "global"

    async def test_on_time_ignores_shipments_without_a_deadline(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carriers: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Иначе перевозчика наказывали бы за то, что клиент не поставил срок."""
        tenant_a, _ = seeded_tenants
        good, _ = carriers
        # Тридцать отмен подряд: у них нет DeliveryOutcome, значит и срока.
        await seed_shipments(tenant_a, good, count=60, on_time=30, cancelled=30)
        await recalculate(tenant_a)

        row = by_code((await client.get("/v1/analytics/carriers", headers=headers)).json(), "good")
        # Тридцать доставленных, все в срок: доля равна единице,
        # а не половине от шестидесяти.
        assert float(row["components"]["on_time_rate"]) == pytest.approx(1.0, abs=0.001)


class TestSnapshots:
    async def test_recalculating_the_same_period_replaces_the_snapshot(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carriers: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Два разных ответа об одном периоде нельзя ни объяснить, ни применить."""
        tenant_a, _ = seeded_tenants
        good, _ = carriers
        await seed_shipments(tenant_a, good, count=50, on_time=50)
        await recalculate(tenant_a)
        first = by_code(
            (await client.get("/v1/analytics/carriers", headers=headers)).json(), "good"
        )

        await recalculate(tenant_a)
        rows = (await client.get("/v1/analytics/carriers", headers=headers)).json()

        assert len([r for r in rows if r["carrier_code"] == "good"]) == 1
        assert by_code(rows, "good")["score"] == first["score"]

    async def test_the_snapshot_records_the_formula_version(
        self, carriers: tuple[UUID, UUID], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        """FR-7.4: изменение весов не должно переписывать историю."""
        tenant_a, _ = seeded_tenants
        good, _ = carriers
        await seed_shipments(tenant_a, good, count=15, on_time=15)
        await recalculate(tenant_a)

        async with session_scope(tenant_a) as session:
            version = (
                await session.execute(
                    text("SELECT formula_version FROM carrier_score_snapshots LIMIT 1")
                )
            ).scalar_one()
        assert version == FORMULA_VERSION
