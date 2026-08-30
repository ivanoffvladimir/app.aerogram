"""Расчёт через API: параллельный опрос, дедлайн, ошибки строками выдачи.

Перевозчик подменяется поддельным адаптером в реестре: настоящие доступы СДЭК
не получены, а контур перевозчика закрыт сетевой политикой. Проверяется путь
платформы — от учётной записи и сопоставления городов до сохранённой выдачи.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.carriers.base import Capabilities, CarrierAccount, Quote, QuoteRequest
from aerogram.core.models import CarrierAccount as CarrierAccountModel
from aerogram.directories.models import Carrier, City, CityCarrierMap
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.ids import uuid7
from tests.conftest import login

pytestmark = pytest.mark.integration

MOSCOW = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
VLADIVOSTOK = "7b6de6a5-86d0-4735-b11a-499081111af8"
TEST_KEY = "k1:" + "A" * 43 + "="


class FakeCarrier:
    """Поддельный перевозчик с управляемым поведением."""

    name = "Поддельный"
    capabilities = Capabilities(supports_cancel=True)

    def __init__(self, code: str, *, behaviour: str = "ok", delay: float = 0.0) -> None:
        self.code = code
        self._behaviour = behaviour
        self._delay = delay
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
        return [
            Quote(
                service_code="136",
                tariff_code="136",
                service_name="Посылка дверь-дверь",
                price=Decimal("2450.50"),
                currency="RUB",
                transit_days_min=2,
                transit_days_max=3,
                promised_delivery_date=date(2026, 9, 4),
                price_source=acc.price_source,
            ),
            Quote(
                service_code="137",
                tariff_code="137",
                service_name="Посылка склад-склад",
                price=Decimal("1505.00"),
                currency="RUB",
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


RATE_REQUEST = {
    "sender": {"city_fias_id": VLADIVOSTOK, "city_name": "Владивосток"},
    "recipient": {"city_fias_id": MOSCOW, "city_name": "Москва"},
    "places": [{"weight_kg": "12.0", "length_cm": 40, "width_cm": 30, "height_cm": 25}],
    "cargo": {"type": "equipment", "declared_value": "480000.00"},
    "options": {"pickup": True, "delivery_to_door": True},
}


class TestSuccessfulRating:
    async def test_returns_quotes_from_connected_carrier(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry.register(FakeCarrier("fake"))
        response = await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert len(body["quotes"]) == 2
        assert body["errors"] == []

    async def test_quotes_are_ranked(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry.register(FakeCarrier("fake"))
        body = (await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        ranks = sorted(q["rank"] for q in body["quotes"])
        assert ranks == [1, 2]

    async def test_price_survives_as_decimal_string(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Деньги не должны проходить через float ни на одном участке."""
        registry.register(FakeCarrier("fake"))
        body = (await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        prices = sorted(str(q["price"]) for q in body["quotes"])
        assert prices == ["1505.00", "2450.50"]

    async def test_carrier_city_codes_are_resolved_before_the_call(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Адаптер получает коды городов уже разрешёнными.

        Разрешать их внутри адаптера значило бы дать ему доступ к базе,
        что запрещено (ADR-0005).
        """
        adapter = FakeCarrier("fake")
        registry.register(adapter)
        await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert adapter.seen[0].sender.carrier_city_code == "75"
        assert adapter.seen[0].recipient.carrier_city_code == "44"

    async def test_request_and_quotes_are_persisted(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """FR-1.7: исходные данные для скора и для разбора спорных ситуаций."""
        registry.register(FakeCarrier("fake"))
        body = (await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        tenant_a, _ = carrier_setup
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        async with engine.connect() as conn:
            # Без установленного тенанта RLS не отдаст ни строки — даже
            # владельцу таблицы, потому что политика объявлена FORCE.
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)}
            )
            stored = (
                await conn.execute(
                    text("SELECT count(*) FROM rate_quotes WHERE rate_request_id = :r"),
                    {"r": body["request_id"]},
                )
            ).scalar_one()
        await engine.dispose()
        assert stored == 2


class TestCarrierFailures:
    async def test_carrier_error_becomes_a_row_not_a_failed_request(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """FR-1.4: ошибка одного перевозчика не роняет выдачу."""
        registry.register(FakeCarrier("fake", behaviour="error"))
        response = await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["quotes"] == []
        assert body["errors"][0]["carrier"] == "fake"
        assert body["errors"][0]["message"] == "Направление не обслуживается"

    async def test_adapter_crash_does_not_leak_as_500(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Непредвиденный сбой адаптера — тоже строка выдачи.

        Иначе один криво написанный адаптер обрушивал бы расчёт по всем
        остальным перевозчикам.
        """
        registry.register(FakeCarrier("fake", behaviour="crash"))
        response = await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        assert response.json()["errors"][0]["code"] == "carrier_error"
        # Текст внутреннего исключения наружу не отдаётся.
        assert "что-то пошло не так" not in json.dumps(response.json(), ensure_ascii=False)

    async def test_unregistered_adapter_is_reported_per_carrier(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        # Учётная запись есть, адаптера нет — это состояние платформы,
        # и пользователь должен видеть причину.
        response = await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        assert response.json()["errors"][0]["code"] == "carrier_not_available"

    async def test_slow_carrier_is_cut_off_by_its_own_timeout(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """FR-1.3: таймаут на одного перевозчика, а не на всю выдачу."""
        registry.register(FakeCarrier("fake", behaviour="ok", delay=5.0))
        response = await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["errors"][0]["code"] in {"carrier_timeout", "carrier_error"}
        assert body["duration_ms"] < 6000


class TestNoCarriers:
    async def test_tenant_without_carrier_accounts_gets_empty_result(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        """Пустая выдача — это результат расчёта, а не отказ сервиса."""
        response = await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        assert response.json()["quotes"] == []
        assert response.json()["errors"] == []


class TestValidation:
    async def test_zero_weight_is_rejected_with_field(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        payload = {**RATE_REQUEST, "places": [{**RATE_REQUEST["places"][0], "weight_kg": "0"}]}
        response = await client.post("/api/v1/rates", json=payload, headers=headers)

        assert response.status_code == 422
        assert "weight_kg" in (response.json()["error"]["field"] or "")

    async def test_request_without_places_is_rejected(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        payload = {**RATE_REQUEST, "places": []}
        assert (
            await client.post("/api/v1/rates", json=payload, headers=headers)
        ).status_code == 422

    async def test_unauthorised_request_is_401(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/rates", json=RATE_REQUEST)).status_code == 401


class TestTenantIsolation:
    async def test_another_tenant_sees_no_quotes(
        self,
        client: AsyncClient,
        carrier_setup: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Учётная запись перевозчика принадлежит тенанту.

        Второй тенант не должен считать по чужому договору — это чужие
        персональные цены.
        """
        registry.register(FakeCarrier("fake"))
        other = await login(client, "b@example.com")
        body = (await client.post("/api/v1/rates", json=RATE_REQUEST, headers=other)).json()

        assert body["quotes"] == []
        assert body["errors"] == []


class TestSeveralCarriers:
    """Опрос нескольких перевозчиков: строки выдачи не должны перепутаться."""

    @pytest.fixture
    async def two_carriers(
        self, seeded_tenants: tuple[UUID, UUID], database_url: str
    ) -> tuple[UUID, UUID]:
        """Две учётные записи: у одной учётные данные нечитаемы."""
        tenant_a, _ = seeded_tenants
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        good_id, broken_id = uuid7(), uuid7()
        good_account, broken_account = uuid7(), uuid7()

        cipher = CredentialCipher({"k1": TEST_KEY.split(":", 1)[1]}, "k1")
        good_secret = cipher.encrypt(
            json.dumps({"client_id": "i", "client_secret": "s"}),
            aad=str(good_account).encode(),
        )

        async with factory() as db, db.begin():
            db.add_all(
                [
                    Carrier(id=good_id, code="slow", name="Медленный"),
                    Carrier(id=broken_id, code="broken", name="Сломанный"),
                ]
            )
            await db.flush()
            await db.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)}
            )
            db.add_all(
                [
                    CarrierAccountModel(
                        id=good_account,
                        tenant_id=tenant_a,
                        carrier_id=good_id,
                        mode="own_contract",
                        credentials_encrypted=good_secret,
                        is_active=True,
                    ),
                    CarrierAccountModel(
                        id=broken_account,
                        tenant_id=tenant_a,
                        carrier_id=broken_id,
                        mode="own_contract",
                        # Шифротекст, привязанный к ЧУЖОЙ записи: расшифровать
                        # его нельзя, и учётная запись должна быть пропущена.
                        credentials_encrypted=good_secret,
                        is_active=True,
                    ),
                ]
            )
        await engine.dispose()
        return good_id, broken_id

    async def test_unreadable_credentials_do_not_shift_other_carriers(
        self, client: AsyncClient, headers: dict[str, str], two_carriers: tuple[UUID, UUID]
    ) -> None:
        """Пропуск одной учётной записи не должен сдвигать остальные.

        Строка таймаута собирается по подготовленным записям, а не по
        исходному списку: иначе она назвала бы чужого перевозчика.
        """
        registry.register(FakeCarrier("slow", delay=5.0))
        response = await client.post("/api/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        errors = response.json()["errors"]
        assert [e["carrier"] for e in errors] == ["slow"]
