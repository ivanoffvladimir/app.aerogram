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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.db import session_scope
from aerogram.directories.models import Carrier
from aerogram.intelligence.models import CarrierPlatformBaseline, CarrierScoreSnapshot
from aerogram.intelligence.score import FORMULA_VERSION, MIN_PLATFORM_SAMPLE
from aerogram.intelligence.service import ScoreService
from aerogram.shared.enums import ScoreConfidence, ScoreScope, ShipmentStatus
from aerogram.shared.ids import uuid7
from aerogram.shipments.models import Shipment
from aerogram.tracking.models import DeliveryOutcome, ShipmentEvent
from tests.conftest import login, make_tenant, make_user
from tests.integration.conftest import RATE_REQUEST, FakeCarrier

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
    batch: str = "a",
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
                # ``batch`` разводит номера, когда одному перевозчику досыпают
                # вторую партию: номер уникален в пределах тенанта.
                number=f"S-{carrier_id.hex[-8:]}-{batch}{index}",
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
        await ScoreService(session).recalculate(PERIOD_START, PERIOD_END, tenant_id=tenant_id)


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


class TestTheScoreBelongsToItsTenant:
    """ADR-0017. Скор считается по отправлениям тенанта и виден только ему.

    До этого решения снапшот был платформенным: пересчёт каждого тенанта
    перезаписывал одну и ту же строку, и витрина отдавала всем статистику
    того, кто считался последним, — его долю просрочек, долю инцидентов,
    индекс цены и объём отправлений.
    """

    async def test_another_tenant_does_not_see_the_snapshot(
        self,
        client: AsyncClient,
        carriers: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Размер выборки — это объём отправлений соседа, и он не его дело."""
        tenant_a, _ = seeded_tenants
        good, _ = carriers
        await seed_shipments(tenant_a, good, count=200, on_time=196)
        await recalculate(tenant_a)

        other = await login(client, "b@example.com")
        rows = (await client.get("/v1/analytics/carriers", headers=other)).json()

        assert by_code(rows, "good")["score"] is None
        assert by_code(rows, "good")["confidence"] == "insufficient"
        assert by_code(rows, "good")["sample_size"] == 0

    async def test_recalculating_one_tenant_does_not_overwrite_another(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carriers: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Ключ уникальности без тенанта и приводил к перезаписи."""
        tenant_a, tenant_b = seeded_tenants
        good, _ = carriers
        # У первого перевозчик возит хорошо, у второго — плохо.
        await seed_shipments(tenant_a, good, count=200, on_time=196)
        await seed_shipments(tenant_b, good, count=200, on_time=40, incidents=60)
        await recalculate(tenant_a)
        await recalculate(tenant_b)

        mine = by_code((await client.get("/v1/analytics/carriers", headers=headers)).json(), "good")
        other_headers = await login(client, "b@example.com")
        theirs = by_code(
            (await client.get("/v1/analytics/carriers", headers=other_headers)).json(), "good"
        )

        assert mine["sample_size"] == 200
        assert theirs["sample_size"] == 200
        # Числа у них разные, и ни одно не затёрло другое.
        assert mine["score"] > theirs["score"]

    async def test_a_snapshot_cannot_be_written_for_someone_else(
        self, carriers: tuple[UUID, UUID], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        """RLS проверяет владельца ещё раз, после кода.

        Ошибка в вызывающем коде не должна означать чужую строку в витрине:
        ``WITH CHECK`` не пропустит запись с чужим тенантом.
        """
        tenant_a, tenant_b = seeded_tenants
        good, _ = carriers

        with pytest.raises(Exception) as exc:
            async with session_scope(tenant_a) as session:
                session.add(
                    CarrierScoreSnapshot(
                        id=uuid7(),
                        tenant_id=tenant_b,
                        carrier_id=good,
                        scope_type=ScoreScope.GLOBAL,
                        scope_key="",
                        period_start=PERIOD_START,
                        period_end=PERIOD_END,
                        sample_size=1,
                        confidence=ScoreConfidence.INSUFFICIENT,
                        formula_version=FORMULA_VERSION,
                    )
                )
                await session.flush()

        assert "row-level security" in str(exc.value).lower()


class TestTheScoreReachesTheQuote:
    """FR-7.6: скор попадает в выдачу расчёта и остаётся в ней снимком."""

    async def test_the_offer_carries_the_score_of_its_carrier(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Иначе оператор выбирает по цене и сроку, не зная о надёжности."""
        registry.register(FakeCarrier("fake"))
        tenant_a, carrier_id = carrier_setup
        await seed_shipments(tenant_a, carrier_id, count=200, on_time=196)
        await recalculate(tenant_a)

        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        offer = body["offers"][0]
        assert offer["carrier_score"] is not None
        assert offer["confidence"] == "high"

    async def test_a_carrier_without_statistics_gets_no_number(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """«Недостаточно данных» в контракте выражается отсутствием числа.

        Значения ``insufficient`` в схеме ``RateOffer`` нет, и придумывать его
        нельзя: раз числа нет, говорить о доверии к нему нечего.
        """
        registry.register(FakeCarrier("fake"))
        tenant_a, carrier_id = carrier_setup
        await seed_shipments(tenant_a, carrier_id, count=5, on_time=5)
        await recalculate(tenant_a)

        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        offer = body["offers"][0]
        assert offer["carrier_score"] is None
        assert offer["confidence"] is None

    async def test_the_recorded_score_does_not_move_with_the_next_recalculation(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Снимок объясняет решение теми числами, которые были видны тогда."""
        registry.register(FakeCarrier("fake"))
        tenant_a, carrier_id = carrier_setup
        await seed_shipments(tenant_a, carrier_id, count=200, on_time=196)
        await recalculate(tenant_a)
        before = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()
        recorded = before["offers"][0]["carrier_score"]

        # Перевозчик начал возить плохо, и скор пересчитан.
        await seed_shipments(tenant_a, carrier_id, count=200, on_time=0, incidents=150, batch="b")
        await recalculate(tenant_a)

        async with session_scope(tenant_a) as session:
            stored = (
                await session.execute(
                    text("SELECT score_at_quote FROM rate_offers WHERE id = :id"),
                    {"id": before["offers"][0]["id"]},
                )
            ).scalar_one()

        assert stored == recorded, "снимок расчёта пересчитали задним числом"


@pytest.fixture
async def four_tenants(
    seeded_tenants: tuple[UUID, UUID], database_url: str
) -> tuple[UUID, UUID, UUID, UUID]:
    """Четыре клиента: троих хватает на порог обезличенности, четвёртый новый.

    Именно четвёртый и есть предмет проверки: он ещё не отправлял ничего
    и обязан увидеть оценку перевозчиков с первого дня.
    """
    tenant_a, tenant_b = seeded_tenants
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_c, tenant_d = uuid7(), uuid7()
    async with factory() as db, db.begin():
        db.add_all([make_tenant(tenant_c, "Третий"), make_tenant(tenant_d, "Новичок")])
        await db.flush()
        for tenant_id, email in ((tenant_c, "c@example.com"), (tenant_d, "d@example.com")):
            await db.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            db.add(make_user(tenant_id, email))
            await db.flush()
    await engine.dispose()
    return tenant_a, tenant_b, tenant_c, tenant_d


async def build_baseline(tenants: tuple[UUID, ...]) -> list[CarrierPlatformBaseline]:
    """Собрать свод так, как это делает фоновая задача: обходом тенантов.

    Наблюдения читаются под каждым тенантом отдельно (RLS никуда не делась),
    складываются в приложении и записываются одной транзакцией.
    """
    per_tenant = []
    for tenant_id in tenants:
        async with session_scope(tenant_id) as session:
            per_tenant.append(await ScoreService(session).observations(PERIOD_START, PERIOD_END))
    async with session_scope() as session:
        return await ScoreService(session).save_baselines(PERIOD_START, PERIOD_END, per_tenant)


class TestPlatformBaselineIsBuiltAcrossTenants:
    """Свод собирается обходом тенантов и только при трёх клиентах (ADR-0026)."""

    async def test_three_tenants_make_a_baseline(
        self, carriers: tuple[UUID, UUID], four_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        good, _ = carriers
        a, b, c, _ = four_tenants
        for tenant_id in (a, b, c):
            await seed_shipments(tenant_id, good, count=20, on_time=18)

        saved = await build_baseline((a, b, c))

        assert len(saved) == 1
        assert saved[0].carrier_id == good
        assert saved[0].tenants_count == 3
        assert saved[0].sample_size == 60

    async def test_two_tenants_make_no_baseline(
        self, carriers: tuple[UUID, UUID], four_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """Свод по двум клиентам это статистика каждого из них, выданная
        второму: он вычитает свои числа и получает чужие почти точно."""
        good, _ = carriers
        a, b, _, _ = four_tenants
        for tenant_id in (a, b):
            await seed_shipments(tenant_id, good, count=100, on_time=90)

        assert await build_baseline((a, b)) == []

    async def test_too_few_shipments_make_no_baseline(
        self, carriers: tuple[UUID, UUID], four_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """Три клиента по три отправления — анонимно, но ничего не значит."""
        good, _ = carriers
        a, b, c, _ = four_tenants
        for tenant_id in (a, b, c):
            await seed_shipments(tenant_id, good, count=3, on_time=3)

        assert await build_baseline((a, b, c)) == []

    async def test_the_database_refuses_an_identifying_row(
        self, carriers: tuple[UUID, UUID], database_url: str
    ) -> None:
        """Второй рубеж: даже мимо кода такую строку записать нельзя.

        Порог живёт и в приложении, и в ограничении таблицы. Это не
        дублирование: приложение можно обойти новым кодом, ограничение —
        только миграцией, которую увидит ревью.
        """
        good, _ = carriers
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            with pytest.raises(IntegrityError):
                async with factory() as db, db.begin():
                    db.add(
                        CarrierPlatformBaseline(
                            id=uuid7(),
                            carrier_id=good,
                            period_start=PERIOD_START,
                            period_end=PERIOD_END,
                            tenants_count=2,
                            sample_size=1000,
                            formula_version=FORMULA_VERSION,
                        )
                    )
        finally:
            await engine.dispose()

    async def test_the_baseline_carries_neither_tenant_nor_price(
        self, session: AsyncSession
    ) -> None:
        """Состав таблицы, а не значения: колонка, добавленная «на будущее»,
        утечёт ровно тогда, когда её кто-нибудь заполнит."""
        columns = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'carrier_platform_baselines'"
                    )
                )
            ).all()
        }
        assert "tenant_id" not in columns
        assert not [name for name in columns if "price" in name or "cost" in name]


class TestANewClientSeesTheScore:
    """Ради этого свод и существует (решение человека от 5 сентября 2026)."""

    async def test_a_score_exists_before_the_first_shipment(
        self,
        client: AsyncClient,
        carriers: tuple[UUID, UUID],
        four_tenants: tuple[UUID, UUID, UUID, UUID],
    ) -> None:
        good, _ = carriers
        a, b, c, newcomer = four_tenants
        for tenant_id in (a, b, c):
            await seed_shipments(tenant_id, good, count=20, on_time=18)
        await build_baseline((a, b, c))
        await recalculate(newcomer)

        headers = await login(client, "d@example.com")
        rows = (await client.get("/v1/analytics/carriers", headers=headers)).json()

        row = by_code(rows, "good")
        assert row["score"] is not None
        assert row["basis"] == "platform"
        # Своих отправлений нет ни одного, и это видно рядом с числом.
        assert row["sample_size"] == 0
        assert row["platform_sample_size"] == 60

    async def test_the_baseline_ranks_carriers_for_a_new_client(
        self,
        client: AsyncClient,
        carriers: tuple[UUID, UUID],
        four_tenants: tuple[UUID, UUID, UUID, UUID],
    ) -> None:
        """Главное требование: рейтинг существует с первого дня.

        Приор, общий на всех перевозчиков, дал бы здесь одинаковые числа,
        и выбирать новому клиенту было бы не из чего.
        """
        good, bad = carriers
        a, b, c, newcomer = four_tenants
        for tenant_id in (a, b, c):
            await seed_shipments(tenant_id, good, count=20, on_time=20)
            await seed_shipments(tenant_id, bad, count=20, on_time=2, incidents=10, batch="b")
        await build_baseline((a, b, c))
        await recalculate(newcomer)

        headers = await login(client, "d@example.com")
        rows = (await client.get("/v1/analytics/carriers", headers=headers)).json()

        assert by_code(rows, "good")["score"] > by_code(rows, "bad")["score"]

    async def test_own_experience_moves_the_number_away_from_the_platform(
        self,
        client: AsyncClient,
        carriers: tuple[UUID, UUID],
        four_tenants: tuple[UUID, UUID, UUID, UUID],
    ) -> None:
        """Свой опыт вытесняет платформенный, а не игнорируется.

        Клиент, у которого перевозчик срывает срок, обязан увидеть это
        у себя, даже если у остальных тот же перевозчик возит хорошо.
        """
        good, _ = carriers
        a, b, c, newcomer = four_tenants
        for tenant_id in (a, b, c):
            await seed_shipments(tenant_id, good, count=20, on_time=20)
        await build_baseline((a, b, c))

        await recalculate(newcomer)
        headers = await login(client, "d@example.com")
        before = by_code(
            (await client.get("/v1/analytics/carriers", headers=headers)).json(), "good"
        )

        await seed_shipments(newcomer, good, count=60, on_time=0, batch="own")
        await recalculate(newcomer)
        after = by_code(
            (await client.get("/v1/analytics/carriers", headers=headers)).json(), "good"
        )

        assert after["score"] < before["score"]
        assert after["basis"] == "mixed"
        assert after["sample_size"] == 60

    async def test_a_carrier_nobody_ships_with_still_has_no_score(
        self,
        client: AsyncClient,
        carriers: tuple[UUID, UUID],
        four_tenants: tuple[UUID, UUID, UUID, UUID],
    ) -> None:
        """Свод не выдумывает: перевозчик без отправлений у кого бы то ни было
        остаётся без числа, а не получает середину шкалы."""
        good, _ = carriers
        a, b, c, newcomer = four_tenants
        for tenant_id in (a, b, c):
            await seed_shipments(tenant_id, good, count=20, on_time=18)
        await build_baseline((a, b, c))
        await recalculate(newcomer)

        headers = await login(client, "d@example.com")
        rows = (await client.get("/v1/analytics/carriers", headers=headers)).json()

        assert by_code(rows, "bad")["score"] is None
        assert by_code(rows, "bad")["basis"] == "none"
        assert by_code(rows, "bad")["confidence"] == "insufficient"

    async def test_the_platform_sample_is_at_least_the_threshold(
        self, carriers: tuple[UUID, UUID], four_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        good, _ = carriers
        a, b, c, _ = four_tenants
        for tenant_id in (a, b, c):
            await seed_shipments(tenant_id, good, count=20, on_time=18)

        saved = await build_baseline((a, b, c))
        assert saved[0].sample_size >= MIN_PLATFORM_SAMPLE
