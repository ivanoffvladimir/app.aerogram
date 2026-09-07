"""Архив отправлений: поиск по периоду (ADR-0030).

Отправления и их печатные формы хранятся не менее пяти лет — это первичные
документы, и срок у них не наш. Пять лет без отбора по периоду означают
список, который листается только перебором, поэтому «хранить» без «найти»
обязанности не исполняет.

Проверяется то, что ошибётся молча: границы периода включительные с обеих
сторон и считаются в часовом поясе ТЕНАНТА. Отправление, созданное второго
апреля в два часа ночи по Москве, в UTC создано первого — и отбор «за апрель»
по UTC потерял бы его. Оператор такую потерю не заметит: список окажется
на строку короче, чем в бухгалтерии.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

from aerogram.carriers import registry
from aerogram.shipments.models import Shipment
from tests.integration.test_shipments_api import ShippingCarrier, _create, _decision

pytestmark = pytest.mark.asyncio

#: Второе апреля, 01:30 по Москве. В UTC — первое апреля, 22:30.
#: Ровно та строка, которую отбор по UTC отнёс бы к марту.
MOSCOW_NIGHT = datetime(2026, 4, 1, 22, 30, tzinfo=UTC)


async def _created_at(database_url: str, tenant_id: UUID, moment: datetime) -> None:
    """Сдвинуть время создания отправлений тенанта.

    Прямо в базе: ждать пять лет тест не может, а поле неизменяемо
    по смыслу, и API его менять не даёт — и правильно делает.
    """
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            await conn.execute(update(Shipment).values(created_at=moment))
    finally:
        await engine.dispose()


async def _numbers(client: AsyncClient, headers: dict[str, str], query: str) -> list[str]:
    response = await client.get(f"/v1/shipments?{query}", headers=headers)
    assert response.status_code == 200, response.text
    return [item["number"] for item in response.json()["items"]]


@pytest.fixture
def carrier(carrier_setup: tuple[UUID, UUID]) -> None:
    registry.register(ShippingCarrier("fake"))


async def _shipment(client: AsyncClient, headers: dict[str, str], key: str) -> dict[str, str]:
    """Оформленное отправление: расчёт → решение → заказ у перевозчика."""
    decision_id = await _decision(client, headers, f"d-{key}")
    created = await _create(client, headers, decision_id, key)
    assert created.status_code == 201, created.text
    return dict(created.json())


class TestPeriod:
    async def test_a_window_around_the_shipment_finds_it(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: None,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        created = await _shipment(client, headers, "arch-1")
        await _created_at(database_url, carrier_setup[0], MOSCOW_NIGHT)

        found = await _numbers(client, headers, "from=2026-04-01&to=2026-04-30")
        assert found == [created["number"]]

    async def test_a_window_before_it_finds_nothing(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: None,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        await _shipment(client, headers, "arch-1")
        await _created_at(database_url, carrier_setup[0], MOSCOW_NIGHT)

        assert await _numbers(client, headers, "from=2026-01-01&to=2026-03-31") == []

    async def test_the_right_edge_includes_the_whole_day(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: None,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """«По второе апреля» человек понимает как «включая второе целиком».

        Граница по началу тех же суток потеряла бы всё, что создано днём,
        — и потеряла бы молча.
        """
        created = await _shipment(client, headers, "arch-1")
        await _created_at(database_url, carrier_setup[0], datetime(2026, 4, 2, 20, 0, tzinfo=UTC))

        found = await _numbers(client, headers, "from=2026-04-02&to=2026-04-02")
        assert found == [created["number"]]

    async def test_the_day_is_the_tenants_day_not_utc(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: None,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Ночь по Москве — предыдущие сутки по UTC.

        Отбор «за второе апреля» обязан найти отправление, созданное
        второго в 01:30 по Москве, и не найти его за первое: бухгалтерия
        считает по местному дню, а не по Гринвичу.
        """
        created = await _shipment(client, headers, "arch-1")
        await _created_at(database_url, carrier_setup[0], MOSCOW_NIGHT)

        assert await _numbers(client, headers, "from=2026-04-02&to=2026-04-02") == [
            created["number"]
        ]
        assert await _numbers(client, headers, "from=2026-04-01&to=2026-04-01") == []

    async def test_only_one_edge_is_allowed(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: None,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """«Всё до конца марта» и «всё с апреля» — обычные вопросы к архиву."""
        await _shipment(client, headers, "arch-1")
        await _created_at(database_url, carrier_setup[0], MOSCOW_NIGHT)

        assert len(await _numbers(client, headers, "from=2026-04-01")) == 1
        assert await _numbers(client, headers, "to=2026-03-31") == []

    async def test_the_period_combines_with_the_other_filters(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: None,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Иначе поиск по архиву сводился бы к одному условию за раз."""
        created = await _shipment(client, headers, "arch-1")
        await _created_at(database_url, carrier_setup[0], MOSCOW_NIGHT)

        query = f"from=2026-04-01&to=2026-04-30&q={created['number']}"
        assert await _numbers(client, headers, query) == [created["number"]]
        assert await _numbers(client, headers, "from=2026-04-01&to=2026-04-30&q=AG-NOPE") == []

    async def test_no_period_means_everything(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier: None,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Отсутствие фильтра не должно превращаться в пустой архив."""
        await _shipment(client, headers, "arch-1")
        await _created_at(database_url, carrier_setup[0], MOSCOW_NIGHT)

        assert len(await _numbers(client, headers, "page=1")) == 1
