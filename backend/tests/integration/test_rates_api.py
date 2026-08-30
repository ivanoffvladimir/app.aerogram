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
from aerogram.shared.money import Money
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


class TestSuccessfulRating:
    async def test_returns_quotes_from_connected_carrier(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry.register(FakeCarrier("fake"))
        response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["offers"]) == 2
        assert body["failures"] == []
        assert body["no_deadline_match"] is False

    async def test_every_offer_is_eligible_without_a_deadline(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Без дедлайна отсекать нечем: пригодны все предложения."""
        registry.register(FakeCarrier("fake"))
        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert all(o["eligible"] for o in body["offers"])
        assert all(o["ineligibility_reason"] is None for o in body["offers"])

    async def test_price_is_whole_minor_units_end_to_end(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Деньги не проходят через float ни на одном участке пути.

        Проверяется весь путь: адаптер → БД → JSON. Сумма приходит целым
        числом минорных единиц с валютой рядом — как требует схема ``Money``
        контракта (ADR-0011).
        """
        registry.register(FakeCarrier("fake"))
        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        prices = sorted(o["total_cost"]["amount_minor"] for o in body["offers"])
        assert prices == [150_500, 245_050]
        assert all(isinstance(o["total_cost"]["amount_minor"], int) for o in body["offers"])
        assert {o["total_cost"]["currency"] for o in body["offers"]} == {"RUB"}

    async def test_carrier_city_codes_are_resolved_before_the_call(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Адаптер получает коды городов уже разрешёнными.

        Разрешать их внутри адаптера значило бы дать ему доступ к базе,
        что запрещено (ADR-0005).
        """
        adapter = FakeCarrier("fake")
        registry.register(adapter)
        await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert adapter.seen[0].sender.carrier_city_code == "75"
        assert adapter.seen[0].recipient.carrier_city_code == "44"

    async def test_request_and_quotes_are_persisted(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Исходные данные для Carrier Score и разбора спорных ситуаций."""
        registry.register(FakeCarrier("fake"))
        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

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
                    text("SELECT count(*) FROM rate_offers WHERE quote_id = :r"),
                    {"r": body["quote_id"]},
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
        response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["offers"] == []
        assert body["failures"][0]["carrier_code"] == "fake"
        assert body["failures"][0]["message"] == "Направление не обслуживается"

    async def test_adapter_crash_does_not_leak_as_500(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Непредвиденный сбой адаптера — тоже строка выдачи.

        Иначе один криво написанный адаптер обрушивал бы расчёт по всем
        остальным перевозчикам.
        """
        registry.register(FakeCarrier("fake", behaviour="crash"))
        response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        assert response.json()["failures"][0]["code"] == "carrier_error"
        # Текст внутреннего исключения наружу не отдаётся.
        assert "что-то пошло не так" not in json.dumps(response.json(), ensure_ascii=False)

    async def test_unregistered_adapter_is_reported_per_carrier(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        # Учётная запись есть, адаптера нет — это состояние платформы,
        # и пользователь должен видеть причину.
        response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        assert response.json()["failures"][0]["code"] == "carrier_not_available"

    async def test_slow_carrier_is_cut_off_by_its_own_timeout(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """FR-1.3: таймаут на одного перевозчика, а не на всю выдачу."""
        registry.register(FakeCarrier("fake", behaviour="ok", delay=5.0))
        response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["failures"][0]["code"] in {"carrier_timeout", "carrier_error"}


class TestNoCarriers:
    async def test_tenant_without_carrier_accounts_gets_empty_result(
        self, client: AsyncClient, headers: dict[str, str], seeded_tenants: tuple[UUID, UUID]
    ) -> None:
        """Пустая выдача — это результат расчёта, а не отказ сервиса."""
        response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        assert response.json()["offers"] == []
        assert response.json()["failures"] == []


class TestValidation:
    async def test_zero_weight_is_rejected_with_field(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        payload = {
            **RATE_REQUEST,
            "packages": [{**RATE_REQUEST["packages"][0], "weight_grams": 0}],
        }
        response = await client.post("/v1/rates", json=payload, headers=headers)

        assert response.status_code == 422
        assert "weight_grams" in (response.json()["error"]["field"] or "")

    async def test_request_without_packages_is_rejected(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        payload = {**RATE_REQUEST, "packages": []}
        assert (await client.post("/v1/rates", json=payload, headers=headers)).status_code == 422

    async def test_unauthorised_request_is_401(self, client: AsyncClient) -> None:
        assert (await client.post("/v1/rates", json=RATE_REQUEST)).status_code == 401


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
        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=other)).json()

        assert body["offers"] == []
        assert body["failures"] == []


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
        response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)

        assert response.status_code == 200
        errors = response.json()["failures"]
        assert [e["carrier_code"] for e in errors] == ["slow"]


class TestDeadline:
    """Дедлайн — жёсткое ограничение активной рекомендации (продуктовое ТЗ, раздел 7)."""

    async def test_late_offers_stay_visible_but_marked(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Опоздавшие варианты не скрываются: показываются с причиной.

        Поддельный перевозчик обещает 4 и 8 сентября; дедлайн 5 сентября
        проходит первый и не проходит второй.
        """
        registry.register(FakeCarrier("fake"))
        payload = {**RATE_REQUEST, "deadline": "2026-09-05T12:00:00+03:00"}
        body = (await client.post("/v1/rates", json=payload, headers=headers)).json()

        assert len(body["offers"]) == 2, "опоздавшее предложение пропало из выдачи"
        by_service = {o["service_code"]: o for o in body["offers"]}
        assert by_service["136"]["eligible"] is True
        assert by_service["137"]["eligible"] is False
        assert by_service["137"]["ineligibility_reason"] == "misses_deadline"
        assert body["no_deadline_match"] is False

    async def test_margin_and_lateness_are_never_both_set(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry.register(FakeCarrier("fake"))
        payload = {**RATE_REQUEST, "deadline": "2026-09-05T12:00:00+03:00"}
        body = (await client.post("/v1/rates", json=payload, headers=headers)).json()

        for offer in body["offers"]:
            assert offer["deadline_margin_seconds"] >= 0
            assert offer["lateness_seconds"] >= 0
            assert min(offer["deadline_margin_seconds"], offer["lateness_seconds"]) == 0

    async def test_no_deadline_match_when_nobody_fits(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Отдельный признак, а не пустая выдача: альтернативы всё равно
        показываются, чтобы оператору было из чего выбирать."""
        registry.register(FakeCarrier("fake"))
        payload = {**RATE_REQUEST, "deadline": "2026-09-01T12:00:00+03:00"}
        body = (await client.post("/v1/rates", json=payload, headers=headers)).json()

        assert body["no_deadline_match"] is True
        assert len(body["offers"]) == 2
        assert not any(o["eligible"] for o in body["offers"])

    async def test_without_a_deadline_nothing_is_measured(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry.register(FakeCarrier("fake"))
        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert body["no_deadline_match"] is False
        assert all(o["deadline_margin_seconds"] is None for o in body["offers"])


class TestCarrierFilters:
    async def test_blacklist_beats_whitelist(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Перевозчик в обоих списках исключается: иначе запрет ничего
        не гарантирует."""
        _, carrier_id = carrier_setup
        registry.register(FakeCarrier("fake"))
        payload = {
            **RATE_REQUEST,
            "carrier_whitelist": [str(carrier_id)],
            "carrier_blacklist": [str(carrier_id)],
        }
        body = (await client.post("/v1/rates", json=payload, headers=headers)).json()

        assert body["offers"] == []
        assert body["failures"] == []

    async def test_whitelist_keeps_only_the_named_carrier(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry.register(FakeCarrier("fake"))
        payload = {**RATE_REQUEST, "carrier_whitelist": [str(uuid7())]}
        body = (await client.post("/v1/rates", json=payload, headers=headers)).json()

        assert body["offers"] == []
