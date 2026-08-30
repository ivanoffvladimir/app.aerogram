"""Общие фикстуры интеграционных тестов.

Здесь живёт то, что нужно больше чем одному файлу: поддельный перевозчик,
подготовленный тенант с учётной записью и эталонное тело запроса расчёта.
pytest находит фикстуры conftest сам — импортировать их по имени из соседнего
теста нельзя: имя фикстуры затенялось бы аргументом тестовой функции.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.carriers.base import (
    Capabilities,
    CarrierAccount,
    Quote,
    QuoteRequest,
    RawEvent,
    ShipmentRequest,
    ShipmentResult,
)
from aerogram.core.models import CarrierAccount as CarrierAccountModel
from aerogram.directories.models import Carrier, City, CityCarrierMap
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.ids import uuid7
from aerogram.shared.money import Money
from tests.conftest import login

MOSCOW = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
VLADIVOSTOK = "7b6de6a5-86d0-4735-b11a-499081111af8"
TEST_KEY = "k1:" + "A" * 43 + "="


class FakeCarrier:
    """Поддельный перевозчик с управляемым поведением."""

    name = "Поддельный"
    capabilities = Capabilities(supports_cancel=True)

    def __init__(
        self,
        code: str,
        *,
        behaviour: str = "ok",
        delay: float = 0.0,
        prices: tuple[int, ...] | None = None,
    ) -> None:
        """``prices`` — суммы в минорных единицах, по одному тарифу на каждую.

        Нужны там, где проверяется порядок выдачи: с одинаковыми ценами
        у двух перевозчиков сортировку по стоимости не отличить от случайности.
        """
        self.code = code
        self._behaviour = behaviour
        self._delay = delay
        self._prices = prices
        self.seen: list[QuoteRequest] = []

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        self.seen.append(req)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._behaviour == "error":
            from aerogram.shared.errors import CarrierValidationError

            raise CarrierValidationError("Направление не обслуживается", carrier_code=self.code)
        if self._behaviour == "crash":
            raise RuntimeError("что-то пошло не так внутри адаптера")
        if self._prices is not None:
            return [
                Quote(
                    service_code=f"p{index}",
                    tariff_code=f"p{index}",
                    service_name=f"Тариф {index}",
                    price=Money(minor, "RUB"),
                    transit_days_min=2,
                    transit_days_max=3,
                    promised_delivery_date=date(2026, 9, 4),
                    price_source=acc.price_source,
                )
                for index, minor in enumerate(self._prices)
            ]
        return [
            Quote(
                service_code="136",
                tariff_code="136",
                service_name="Посылка дверь-дверь",
                price=Money(245_050, "RUB"),
                transit_days_min=2,
                transit_days_max=3,
                promised_delivery_date=date(2026, 9, 4),
                price_source=acc.price_source,
            ),
            Quote(
                service_code="137",
                tariff_code="137",
                service_name="Посылка склад-склад",
                price=Money(150_500, "RUB"),
                transit_days_min=3,
                transit_days_max=5,
                promised_delivery_date=date(2026, 9, 8),
                price_source=acc.price_source,
            ),
        ]


@pytest.fixture(autouse=True)
def clean_registry() -> AsyncIterator[None]:
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def headers(client: AsyncClient, seeded_tenants: tuple[UUID, UUID]) -> dict[str, str]:
    return await login(client, "a@example.com")


@pytest.fixture
async def carrier_setup(seeded_tenants: tuple[UUID, UUID], database_url: str) -> tuple[UUID, UUID]:
    """Перевозчик, города, сопоставление и учётная запись тенанта."""
    tenant_a, _ = seeded_tenants
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    carrier_id = uuid7()

    cipher = CredentialCipher({"k1": TEST_KEY.split(":", 1)[1]}, "k1")
    account_id = uuid7()
    encrypted = cipher.encrypt(
        json.dumps({"client_id": "test-id", "client_secret": "test-secret"}),
        aad=str(account_id).encode(),
    )

    async with factory() as db, db.begin():
        db.add(Carrier(id=carrier_id, code="fake", name="Поддельный"))
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
                    city_fias_id=MOSCOW,
                    carrier_city_code="44",
                    match_method="fias",
                    is_confirmed=True,
                ),
                CityCarrierMap(
                    id=uuid7(),
                    carrier_id=carrier_id,
                    city_fias_id=VLADIVOSTOK,
                    carrier_city_code="75",
                    match_method="fias",
                    is_confirmed=True,
                ),
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
                is_sandbox=True,
                is_active=True,
            )
        )
    await engine.dispose()
    return tenant_a, carrier_id


#: Тело запроса по схеме ``RateRequest`` контракта. Города приходят названиями:
#: идентификатора ФИАС в контракте нет, разрешение — наша забота.
RATE_REQUEST = {
    "origin": {
        "country": "RU",
        "city": "Владивосток",
        "address_line": "ул. Примерная, 1",
    },
    "destination": {
        "country": "RU",
        "city": "Москва",
        "address_line": "ул. Получателя, 10",
    },
    "packages": [{"weight_grams": 12_000, "length_mm": 400, "width_mm": 300, "height_mm": 250}],
    "cargo_value": {"amount_minor": 48_000_000, "currency": "RUB"},
    "cargo_type": "equipment",
    "additional_services": ["pickup", "door_delivery"],
    "strategy": "optimal",
}


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
