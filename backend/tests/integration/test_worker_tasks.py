"""Фоновые задачи: обход тенантов и устойчивость к сбою одного из них.

У фоновой задачи нет запроса, а значит и тенанта, но роль приложения работает
под `FORCE ROW LEVEL SECURITY` и без установленного `app.tenant_id` не видит
ничего. Проверяется, что обход тенантов действительно работает и что один
испорченный тенант не замораживает трекинг всей платформы: такой сбой
незаметен — статусы просто перестают обновляться.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.carriers.base import (
    CarrierAccount,
    CarrierTerminalRow,
    RawEvent,
    RefCatalog,
    ShipmentResult,
)
from aerogram.core.models import CarrierAccount as CarrierAccountModel
from aerogram.core.models import Tenant
from aerogram.db import session_scope
from aerogram.directories.models import Carrier
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.enums import ShipmentStatus, TenantStatus
from aerogram.shared.ids import uuid7
from aerogram.shipments.models import Shipment
from aerogram.worker import tasks
from tests.integration.conftest import TEST_KEY, FakeCarrier

pytestmark = pytest.mark.asyncio

PAST = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


class PollingCarrier(FakeCarrier):
    """Перевозчик с историей событий и управляемым сбоем."""

    def __init__(self, code: str = "cdek") -> None:
        super().__init__(code)
        self.events: list[RawEvent] = []
        self.fail_for: set[str] = set()
        self.asked: list[str] = []

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        self.asked.append(ext_id)
        if ext_id in self.fail_for:
            raise RuntimeError("перевозчик ответил мусором")
        return list(self.events)

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        return None


@pytest.fixture
def carrier(app: object) -> PollingCarrier:
    """Подменить адаптер СДЭК: карта статусов есть только для настоящих ТК."""
    registry._reset_for_tests()
    adapter = PollingCarrier()
    registry.register(adapter)
    return adapter


@pytest.fixture
async def two_tenants_with_shipments(
    seeded_tenants: tuple[UUID, UUID], database_url: str
) -> tuple[UUID, UUID]:
    """У каждого тенанта по отправлению, которому пора опросить статус."""
    tenant_a, tenant_b = seeded_tenants
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    carrier_id = uuid7()
    cipher = CredentialCipher({"k1": TEST_KEY.split(":", 1)[1]}, "k1")

    async with factory() as db, db.begin():
        db.add(Carrier(id=carrier_id, code="cdek", name="СДЭК"))
        await db.flush()
        for tenant_id, suffix in ((tenant_a, "A"), (tenant_b, "B")):
            await db.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            account_id = uuid7()
            db.add(
                CarrierAccountModel(
                    id=account_id,
                    tenant_id=tenant_id,
                    carrier_id=carrier_id,
                    mode="own_contract",
                    credentials_encrypted=cipher.encrypt(
                        json.dumps({"client_id": "i", "client_secret": "s"}),
                        aad=str(account_id).encode(),
                    ),
                    is_active=True,
                )
            )
            db.add(
                Shipment(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    number=f"AG-{suffix}",
                    carrier_id=carrier_id,
                    carrier_account_id=account_id,
                    external_id=f"EXT-{suffix}",
                    status=ShipmentStatus.CREATED,
                    currency="RUB",
                    next_poll_at=PAST,
                )
            )
            # Сброс на каждом тенанте: иначе строки обоих уедут в базу в конце
            # транзакции, когда app.tenant_id указывает уже на второго,
            # и политика отвергнет строки первого.
            await db.flush()
    await engine.dispose()
    return tenant_a, tenant_b


class TestTenantWalk:
    async def test_every_tenant_is_polled_not_just_the_first(
        self, two_tenants_with_shipments: tuple[UUID, UUID], carrier: PollingCarrier
    ) -> None:
        """Без обхода тенантов задача не увидела бы ни одного отправления."""
        carrier.events = [RawEvent(occurred_at=PAST, status_raw="TAKEN_BY_COURIER")]

        result = await tasks._for_each_tenant("test", tasks._poll_tenant)

        assert sorted(carrier.asked) == ["EXT-A", "EXT-B"]
        assert result["handled"] == 2
        assert result["failed_tenants"] == 0

    async def test_a_suspended_tenant_is_left_alone(
        self,
        two_tenants_with_shipments: tuple[UUID, UUID],
        carrier: PollingCarrier,
        database_url: str,
    ) -> None:
        """Платформа не ходит к перевозчикам за того, кто не оплатил."""
        _, tenant_b = two_tenants_with_shipments
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        async with engine.begin() as conn:
            await conn.execute(
                update(Tenant).where(Tenant.id == tenant_b).values(status=TenantStatus.SUSPENDED)
            )
        await engine.dispose()

        await tasks._for_each_tenant("test", tasks._poll_tenant)

        assert carrier.asked == ["EXT-A"]

    async def test_a_failing_carrier_call_does_not_stop_the_walk(
        self, two_tenants_with_shipments: tuple[UUID, UUID], carrier: PollingCarrier
    ) -> None:
        """Сквозная проверка: сбой у одного тенанта не лишает обновлений другого.

        Ловит его при этом заслон по отправлению — до заслона по тенантам
        дело не доходит. Тот проверяется отдельно, ниже.
        """
        carrier.fail_for = {"EXT-A"}
        carrier.events = [RawEvent(occurred_at=PAST, status_raw="TAKEN_BY_COURIER")]

        result = await tasks._for_each_tenant("test", tasks._poll_tenant)

        assert sorted(carrier.asked) == ["EXT-A", "EXT-B"]
        # Отправление тенанта B обновилось, несмотря на сбой у A.
        assert result["handled"] == 1

    async def test_one_broken_shipment_does_not_stop_the_rest_of_the_tenant(
        self, two_tenants_with_shipments: tuple[UUID, UUID], carrier: PollingCarrier
    ) -> None:
        """У соседнего отправления может быть другой перевозчик.

        Заслон здесь отдельный от заслона по тенантам: без него первое же
        сбойное отправление лишало бы обновлений весь остаток списка.
        """
        tenant_a, _ = two_tenants_with_shipments
        async with session_scope(tenant_a) as session:
            session.add(
                Shipment(
                    id=uuid7(),
                    tenant_id=tenant_a,
                    number="AG-A2",
                    carrier_id=(
                        await session.execute(
                            text("SELECT carrier_id FROM shipments WHERE number = 'AG-A'")
                        )
                    ).scalar_one(),
                    carrier_account_id=(
                        await session.execute(
                            text("SELECT carrier_account_id FROM shipments WHERE number = 'AG-A'")
                        )
                    ).scalar_one(),
                    external_id="EXT-A2",
                    status=ShipmentStatus.CREATED,
                    currency="RUB",
                    next_poll_at=PAST,
                )
            )

        carrier.fail_for = {"EXT-A"}
        carrier.events = [RawEvent(occurred_at=PAST, status_raw="TAKEN_BY_COURIER")]

        await tasks._for_each_tenant("test", tasks._poll_tenant)

        assert "EXT-A2" in carrier.asked, "после сбоя перестали опрашивать остальные"
        async with session_scope(tenant_a) as session:
            status = (
                await session.execute(text("SELECT status FROM shipments WHERE number = 'AG-A2'"))
            ).scalar_one()
        assert status == ShipmentStatus.OUT_FOR_DELIVERY

    async def test_a_tenant_level_failure_is_contained(self) -> None:
        """Второй заслон: сбой вне цикла по отправлениям.

        Например, недоступная строка тенанта или испорченная настройка.
        Без него один клиент заморозил бы трекинг всей платформы, а сбой
        такого рода незаметен: статусы просто перестают обновляться.
        """
        seen: list[UUID] = []

        async def action(tenant_id: UUID) -> int:
            seen.append(tenant_id)
            if len(seen) == 1:
                raise RuntimeError("тенант сломан целиком")
            return 1

        result = await tasks._for_each_tenant("test", action)

        assert len(seen) == 2, "обход прервался на первом же сбое"
        assert result == {"handled": 1, "failed_tenants": 1}


class TestPolling:
    async def test_polling_advances_the_status_and_the_next_check(
        self, two_tenants_with_shipments: tuple[UUID, UUID], carrier: PollingCarrier
    ) -> None:
        tenant_a, _ = two_tenants_with_shipments
        carrier.events = [RawEvent(occurred_at=PAST, status_raw="TAKEN_BY_COURIER")]

        await tasks._for_each_tenant("test", tasks._poll_tenant)

        async with session_scope(tenant_a) as session:
            row = (
                await session.execute(
                    text("SELECT status, next_poll_at FROM shipments WHERE number = 'AG-A'")
                )
            ).one()
        assert row.status == ShipmentStatus.OUT_FOR_DELIVERY
        # На доставке опрашиваем каждые полчаса (FR-3.2), а не через час.
        assert row.next_poll_at is not None

    async def test_a_shipment_not_yet_due_is_left_alone(
        self, two_tenants_with_shipments: tuple[UUID, UUID], carrier: PollingCarrier
    ) -> None:
        """Опрос не по расписанию тратит лимит перевозчика впустую."""
        tenant_a, tenant_b = two_tenants_with_shipments
        for tenant_id in (tenant_a, tenant_b):
            async with session_scope(tenant_id) as session:
                await session.execute(
                    update(Shipment).values(next_poll_at=datetime.now(UTC) + timedelta(hours=1))
                )

        await tasks._for_each_tenant("test", tasks._poll_tenant)

        assert carrier.asked == []


class RefCarrier(FakeCarrier):
    """Перевозчик с управляемой выгрузкой справочника."""

    def __init__(self, code: str = "cdek") -> None:
        super().__init__(code)
        self.catalog = RefCatalog()
        self.calls = 0

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        self.calls += 1
        return self.catalog


def terminal(code: str, city: str = "Москва") -> CarrierTerminalRow:
    return CarrierTerminalRow(
        external_code=code,
        city_name=city,
        address=f"ул. Складская, {code}",
        type="pvz",
        has_card=True,
    )


async def active_codes(carrier_id: UUID, tenant_id: UUID) -> set[str]:
    async with session_scope(tenant_id) as session:
        rows = await session.execute(
            text("SELECT external_code FROM carrier_terminals WHERE carrier_id = :c AND is_active"),
            {"c": carrier_id},
        )
        return set(rows.scalars())


@pytest.fixture
def ref_carrier(app: object) -> RefCarrier:
    registry._reset_for_tests()
    adapter = RefCarrier()
    registry.register(adapter)
    return adapter


class TestReferenceSync:
    """FR-8.3: терминалы и ПВЗ синхронизируются ежесуточно."""

    async def test_the_catalogue_is_written(
        self,
        ref_carrier: RefCarrier,
        two_tenants_with_shipments: tuple[UUID, UUID],
    ) -> None:
        """До этой задачи расписание звало имя, которого не существовало."""
        tenant_a, _ = two_tenants_with_shipments
        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"), terminal("MSK-2")))

        result = await tasks._for_each_tenant("sync", tasks._refs_tenant)

        assert result["failed_tenants"] == 0
        async with session_scope(tenant_a) as session:
            carrier_id = (
                await session.execute(text("SELECT id FROM carriers WHERE code = 'cdek'"))
            ).scalar_one()
        assert await active_codes(carrier_id, tenant_a) == {"MSK-1", "MSK-2"}

    async def test_a_terminal_that_stopped_coming_is_switched_off(
        self,
        ref_carrier: RefCarrier,
        two_tenants_with_shipments: tuple[UUID, UUID],
    ) -> None:
        """Закрытый ПВЗ, который продолжает предлагаться, — это сорванная выдача."""
        tenant_a, _ = two_tenants_with_shipments
        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"), terminal("MSK-2")))
        await tasks._for_each_tenant("sync", tasks._refs_tenant)

        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"),))
        await tasks._for_each_tenant("sync", tasks._refs_tenant)

        async with session_scope(tenant_a) as session:
            carrier_id = (
                await session.execute(text("SELECT id FROM carriers WHERE code = 'cdek'"))
            ).scalar_one()
            gone = (
                await session.execute(
                    text(
                        "SELECT is_active, deactivated_at IS NOT NULL FROM carrier_terminals"
                        " WHERE carrier_id = :c AND external_code = 'MSK-2'"
                    ),
                    {"c": carrier_id},
                )
            ).one()
        # Строка остаётся: её код лежит в уже созданных отправлениях.
        assert gone == (False, True)
        assert await active_codes(carrier_id, tenant_a) == {"MSK-1"}

    async def test_a_terminal_that_came_back_is_switched_on_again(
        self,
        ref_carrier: RefCarrier,
        two_tenants_with_shipments: tuple[UUID, UUID],
    ) -> None:
        """ПВЗ закрывают на ремонт и открывают обратно."""
        tenant_a, _ = two_tenants_with_shipments
        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"), terminal("MSK-2")))
        await tasks._for_each_tenant("sync", tasks._refs_tenant)
        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"),))
        await tasks._for_each_tenant("sync", tasks._refs_tenant)

        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"), terminal("MSK-2")))
        await tasks._for_each_tenant("sync", tasks._refs_tenant)

        async with session_scope(tenant_a) as session:
            carrier_id = (
                await session.execute(text("SELECT id FROM carriers WHERE code = 'cdek'"))
            ).scalar_one()
        assert await active_codes(carrier_id, tenant_a) == {"MSK-1", "MSK-2"}

    async def test_an_incomplete_catalogue_switches_nothing_off(
        self,
        ref_carrier: RefCarrier,
        two_tenants_with_shipments: tuple[UUID, UUID],
    ) -> None:
        """Оборванная страница не означает, что перевозчик закрыл сеть.

        Погасить её целиком дороже, чем показать один закрытый ПВЗ: вернуть
        сеть можно только следующей успешной синхронизацией.
        """
        tenant_a, _ = two_tenants_with_shipments
        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"), terminal("MSK-2")))
        await tasks._for_each_tenant("sync", tasks._refs_tenant)

        ref_carrier.catalog = RefCatalog(terminals=(terminal("MSK-1"),), is_complete=False)
        await tasks._for_each_tenant("sync", tasks._refs_tenant)

        async with session_scope(tenant_a) as session:
            carrier_id = (
                await session.execute(text("SELECT id FROM carriers WHERE code = 'cdek'"))
            ).scalar_one()
        assert await active_codes(carrier_id, tenant_a) == {"MSK-1", "MSK-2"}

    async def test_a_carrier_that_fails_does_not_stop_the_others(
        self,
        ref_carrier: RefCarrier,
        two_tenants_with_shipments: tuple[UUID, UUID],
    ) -> None:
        """У каждого перевозчика свой контур и свои доступы."""

        async def explode(acc: CarrierAccount) -> RefCatalog:
            raise RuntimeError("выгрузка не удалась")

        ref_carrier.fetch_refs = explode  # type: ignore[method-assign]

        result = await tasks._for_each_tenant("sync", tasks._refs_tenant)

        # Тенант не считается упавшим: сбой перевозчика — штатное состояние,
        # он ловится внутри и не роняет обход.
        assert result["failed_tenants"] == 0
        assert result["handled"] == 0
