"""Расчёт через API: параллельный опрос, дедлайн, ошибки строками выдачи.

Перевозчик подменяется поддельным адаптером в реестре: настоящие доступы СДЭК
не получены, а контур перевозчика закрыт сетевой политикой. Проверяется путь
платформы — от учётной записи и сопоставления городов до сохранённой выдачи.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.carriers.base import Capabilities
from aerogram.core.models import CarrierAccount as CarrierAccountModel
from aerogram.directories.models import Carrier
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.ids import uuid7
from tests.conftest import login
from tests.integration.conftest import RATE_REQUEST, TEST_KEY, FakeCarrier

pytestmark = pytest.mark.integration


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


class TestQuoteIsReused:
    """FR-1.6: тот же запрос в пределах срока жизни не опрашивает ТК заново."""

    async def test_the_same_request_returns_the_same_quote(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Перевозчика не спрашивают дважды об одном и том же."""
        carrier = FakeCarrier("fake")
        registry.register(carrier)

        first = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()
        second = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert second == first, "повтор обязан вернуть ровно тот же ответ"
        assert len(carrier.seen) == 1, "перевозчика опросили второй раз"

    async def test_a_different_request_is_calculated_anew(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Иначе изменение веса не меняло бы цену."""
        carrier = FakeCarrier("fake")
        registry.register(carrier)
        heavier = {
            **RATE_REQUEST,
            "packages": [
                {"weight_grams": 30_000, "length_mm": 400, "width_mm": 300, "height_mm": 250}
            ],
        }

        first = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()
        second = (await client.post("/v1/rates", json=heavier, headers=headers)).json()

        assert second["quote_id"] != first["quote_id"]
        assert len(carrier.seen) == 2

    async def test_the_order_of_services_does_not_make_it_another_request(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Список услуг — множество: порядок в нём ничего не значит."""
        carrier = FakeCarrier("fake")
        registry.register(carrier)
        reordered = {**RATE_REQUEST, "additional_services": ["door_delivery", "pickup"]}
        assert reordered["additional_services"] != RATE_REQUEST["additional_services"]

        first = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()
        second = (await client.post("/v1/rates", json=reordered, headers=headers)).json()

        assert second["quote_id"] == first["quote_id"]
        assert len(carrier.seen) == 1

    async def test_the_same_moment_in_another_timezone_is_the_same_request(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """«12:00+03:00» и «09:00Z» — один момент, а не два разных срока."""
        carrier = FakeCarrier("fake")
        registry.register(carrier)
        moscow = {**RATE_REQUEST, "deadline": "2026-09-05T12:00:00+03:00"}
        utc = {**RATE_REQUEST, "deadline": "2026-09-05T09:00:00Z"}

        first = (await client.post("/v1/rates", json=moscow, headers=headers)).json()
        second = (await client.post("/v1/rates", json=utc, headers=headers)).json()

        assert second["quote_id"] == first["quote_id"]
        assert len(carrier.seen) == 1

    async def test_an_expired_quote_is_not_reused(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Просроченная выдача — не выдача: по ней нельзя оформлять."""
        carrier = FakeCarrier("fake")
        registry.register(carrier)
        first = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        # Время двигается в самой выдаче, а не в тесте: подмена часов
        # проверяла бы стенд, а не условие отбора.
        tenant_a, _ = carrier_setup
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)}
            )
            aged = await conn.execute(
                text("UPDATE rate_quotes SET valid_until = now() - interval '1 second'")
            )
        await engine.dispose()
        # Без тенанта RLS не отдала бы ни строки, и тест «прошёл» бы,
        # ничего не состарив.
        assert aged.rowcount == 1

        second = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert second["quote_id"] != first["quote_id"]
        assert len(carrier.seen) == 2

    async def test_a_quote_without_a_single_offer_is_not_reused(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Иначе минутная недоступность ТК растянулась бы на весь срок жизни."""
        registry.register(FakeCarrier("fake", behaviour="error"))
        first = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()
        assert first["offers"] == []

        second = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert second["quote_id"] != first["quote_id"]

    async def test_a_newly_connected_carrier_makes_the_quote_stale(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Иначе подключённый только что перевозчик четверть часа выглядел бы
        сломанным: его нет в выдаче, а расчёт «уже посчитан»."""
        registry.register(FakeCarrier("fake"))
        registry.register(FakeCarrier("second"))
        first = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()
        assert len(first["offers"]) == 2

        tenant_a, _ = carrier_setup
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        carrier_id, account_id = uuid7(), uuid7()
        cipher = CredentialCipher({"k1": TEST_KEY.split(":", 1)[1]}, "k1")
        async with factory() as db, db.begin():
            db.add(Carrier(id=carrier_id, code="second", name="Второй"))
            await db.flush()
            await db.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)}
            )
            db.add(
                CarrierAccountModel(
                    id=account_id,
                    tenant_id=tenant_a,
                    carrier_id=carrier_id,
                    mode="own_contract",
                    credentials_encrypted=cipher.encrypt(
                        json.dumps({"client_id": "i", "client_secret": "s"}),
                        aad=str(account_id).encode(),
                    ),
                    is_active=True,
                )
            )
        await engine.dispose()

        second = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert second["quote_id"] != first["quote_id"]
        assert len(second["offers"]) == 4, "новый перевозчик обязан появиться в выдаче"

    async def test_another_tenant_does_not_get_the_first_ones_quote(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """Отпечаток запроса у двух тенантов один, а цены — их собственные."""
        registry.register(FakeCarrier("fake"))
        first = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        other = await login(client, "b@example.com")
        second = (await client.post("/v1/rates", json=RATE_REQUEST, headers=other)).json()

        assert second["quote_id"] != first["quote_id"]
        # У второго тенанта нет учётной записи перевозчика — и чужих
        # предложений он не видит.
        assert second["offers"] == []


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


class TestOfferOrder:
    """Порядок выдачи не должен зависеть от того, кто ответил первым.

    Перевозчики опрашиваются параллельно, и результаты собирались обходом
    множества ``done``: одна и та же выдача переставлялась от расчёта
    к расчёту без единого изменения данных. Оператор при этом видит, что
    список «прыгает», и перестаёт ему доверять.
    """

    @pytest.fixture
    async def fast_and_slow(
        self, seeded_tenants: tuple[UUID, UUID], database_url: str
    ) -> tuple[UUID, UUID]:
        """Два работающих перевозчика: быстрый дорогой и медленный дешёвый."""
        tenant_a, _ = seeded_tenants
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        cipher = CredentialCipher({"k1": TEST_KEY.split(":", 1)[1]}, "k1")

        quick_id, slow_id = uuid7(), uuid7()
        # Записи заводятся в обратном алфавиту порядке намеренно: так видно,
        # что порядок ответа задаётся сортировкой, а не порядком подключения.
        accounts = {slow_id: uuid7(), quick_id: uuid7()}

        async with factory() as db, db.begin():
            db.add_all(
                [
                    Carrier(id=quick_id, code="quick", name="Быстрый"),
                    Carrier(id=slow_id, code="slow", name="Медленный"),
                ]
            )
            await db.flush()
            await db.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)}
            )
            db.add_all(
                [
                    CarrierAccountModel(
                        id=account_id,
                        tenant_id=tenant_a,
                        carrier_id=carrier_id,
                        mode="own_contract",
                        credentials_encrypted=cipher.encrypt(
                            json.dumps({"client_id": "i", "client_secret": "s"}),
                            aad=str(account_id).encode(),
                        ),
                        is_active=True,
                    )
                    for carrier_id, account_id in accounts.items()
                ]
            )
        await engine.dispose()
        return quick_id, slow_id

    async def test_cheapest_comes_first_even_when_it_answers_last(
        self, client: AsyncClient, headers: dict[str, str], fast_and_slow: tuple[UUID, UUID]
    ) -> None:
        """Дешёвый вариант не уезжает в конец из-за того, что ТК тормозит."""
        registry.register(FakeCarrier("quick", prices=(300_00,)))
        registry.register(FakeCarrier("slow", delay=0.3, prices=(100_00,)))

        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert [o["total_cost"]["amount_minor"] for o in body["offers"]] == [100_00, 300_00]

    async def test_failures_have_a_defined_order_too(
        self, client: AsyncClient, headers: dict[str, str], fast_and_slow: tuple[UUID, UUID]
    ) -> None:
        """Отказы упорядочены по коду, а не по тому, кто раньше подключён.

        Записи заведены в обратном алфавиту порядке: «slow» раньше «quick».
        Если бы порядок доставался от подключения, ответ начинался бы
        с «slow».
        """
        registry.register(FakeCarrier("quick", behaviour="error"))
        registry.register(FakeCarrier("slow", behaviour="error"))

        body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()

        assert [f["carrier_code"] for f in body["failures"]] == ["quick", "slow"]

    async def test_the_same_request_gives_the_same_order(
        self, client: AsyncClient, headers: dict[str, str], fast_and_slow: tuple[UUID, UUID]
    ) -> None:
        """Два одинаковых расчёта подряд — одна и та же последовательность."""
        registry.register(FakeCarrier("quick", prices=(300_00, 150_00)))
        registry.register(FakeCarrier("slow", delay=0.2, prices=(100_00, 220_00)))

        orders = []
        for _ in range(3):
            body = (await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)).json()
            orders.append(
                [(o["carrier_name"], o["total_cost"]["amount_minor"]) for o in body["offers"]]
            )

        assert orders[0] == [
            ("Медленный", 100_00),
            ("Быстрый", 150_00),
            ("Медленный", 220_00),
            ("Быстрый", 300_00),
        ]
        assert orders[1] == orders[0]
        assert orders[2] == orders[0]


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


class TestRobustness:
    """Входные данные, на которых расчёт раньше отвечал 500."""

    async def test_region_of_punctuation_does_not_crash_the_request(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Регион из одной точки не должен ронять весь расчёт.

        Разбор региона брал первое слово по индексу, а у строки без слов
        список пуст — и запрос заканчивался 500 вместо выдачи.
        """
        registry.register(FakeCarrier("fake"))
        payload = {
            **RATE_REQUEST,
            "destination": {**RATE_REQUEST["destination"], "region": "."},
        }
        response = await client.post("/v1/rates", json=payload, headers=headers)

        assert response.status_code == 200, response.text
        assert len(response.json()["offers"]) == 2

    async def test_blank_region_does_not_veto_anything(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Пустой регион — отсутствие уточнения, а не запрет всех городов."""
        registry.register(FakeCarrier("fake"))
        payload = {
            **RATE_REQUEST,
            "destination": {**RATE_REQUEST["destination"], "region": "   "},
        }
        response = await client.post("/v1/rates", json=payload, headers=headers)

        assert response.status_code == 200, response.text
        assert len(response.json()["offers"]) == 2

    async def test_deadline_without_a_timezone_is_a_client_error_not_a_crash(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry.register(FakeCarrier("fake"))
        payload = {**RATE_REQUEST, "deadline": "2026-09-05T12:00:00"}
        response = await client.post("/v1/rates", json=payload, headers=headers)

        assert response.status_code == 422
        assert "deadline" in (response.json()["error"]["field"] or "")


class TestNoDeadlineMatch:
    async def test_not_set_when_nobody_answered(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """«Никто не успевает» и «никто не ответил» — разные состояния.

        Первое требует показать ближайшие альтернативы, второе — разобраться
        с доступностью перевозчиков. Путать их нельзя ни в ответе, ни в снимке.
        """
        registry.register(FakeCarrier("fake", behaviour="error"))
        payload = {**RATE_REQUEST, "deadline": "2026-09-05T12:00:00+03:00"}
        body = (await client.post("/v1/rates", json=payload, headers=headers)).json()

        assert body["offers"] == []
        assert body["failures"] != []
        assert body["no_deadline_match"] is False


#: Лёгкая, но громоздкая коробка: 60 × 50 × 40 см при килограмме. Объёмный вес
#: 120000 / 5000 = 24 кг — в двадцать четыре раза больше фактического, и именно
#: по нему выставит счёт перевозчик.
BULKY = {
    **RATE_REQUEST,
    "packages": [{"weight_grams": 1_000, "length_mm": 600, "width_mm": 500, "height_mm": 400}],
}


class TestChargeableWeight:
    """FR-1.2: платят по большему из весов — фактическому и объёмному.

    Объёмный считается как Д × Ш × В (см) / делитель перевозчика, по умолчанию
    5000. Подменять вес нужно только тому перевозчику, который сам объёмный
    вес не считает: иначе он посчитает его ещё раз, уже поверх подменённого,
    и клиент заплатит дважды за один и тот же объём.
    """

    async def test_a_carrier_that_does_not_count_it_gets_the_volumetric_weight(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Иначе мы считаем по килограмму, а счёт придёт за двадцать четыре."""
        carrier = FakeCarrier("fake")
        carrier.capabilities = Capabilities(supports_cancel=True, computes_volumetric_weight=False)
        registry.register(carrier)

        await client.post("/v1/rates", json=BULKY, headers=headers)

        assert carrier.seen[0].places[0].weight_kg == Decimal("24.000")

    async def test_a_carrier_that_counts_it_gets_the_actual_weight(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Двойной учёт объёма дороже занижения: он завышает цену молча."""
        carrier = FakeCarrier("fake")
        assert carrier.capabilities.computes_volumetric_weight is True
        registry.register(carrier)

        await client.post("/v1/rates", json=BULKY, headers=headers)

        assert carrier.seen[0].places[0].weight_kg == Decimal("1")

    async def test_a_heavy_compact_parcel_keeps_its_actual_weight(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Максимум, а не замена: плотный груз тарифицируется по факту."""
        carrier = FakeCarrier("fake")
        carrier.capabilities = Capabilities(supports_cancel=True, computes_volumetric_weight=False)
        registry.register(carrier)
        dense = {
            **RATE_REQUEST,
            "packages": [
                {"weight_grams": 30_000, "length_mm": 200, "width_mm": 200, "height_mm": 200}
            ],
        }

        await client.post("/v1/rates", json=dense, headers=headers)

        # Объёмный: 20 × 20 × 20 / 5000 = 1.6 кг, фактический 30 кг.
        assert carrier.seen[0].places[0].weight_kg == Decimal("30.000")

    async def test_a_package_without_dimensions_is_not_made_heavier(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Габариты в контракте необязательны, и их отсутствие не повод
        завысить цену."""
        carrier = FakeCarrier("fake")
        carrier.capabilities = Capabilities(supports_cancel=True, computes_volumetric_weight=False)
        registry.register(carrier)
        no_dimensions = {**RATE_REQUEST, "packages": [{"weight_grams": 2_000}]}

        await client.post("/v1/rates", json=no_dimensions, headers=headers)

        assert carrier.seen[0].places[0].weight_kg == Decimal("2.000")
