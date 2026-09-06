"""Правила маршрутизации по API и их действие на расчёт.

Движок правил проверен модульно (``tests/unit/test_routing_rules.py``).
Здесь проверяется то, что модульный тест увидеть не может: правило доходит
до расчёта, запрещённого перевозчика действительно НЕ спрашивают, а его
строка из выдачи не исчезает.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aerogram.carriers import registry
from tests.integration.conftest import MOSCOW, VLADIVOSTOK, FakeCarrier

pytestmark = pytest.mark.asyncio


async def _rate(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    from tests.integration.conftest import RATE_REQUEST

    response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _rule(
    client: AsyncClient,
    headers: dict[str, str],
    conditions: dict[str, Any],
    actions: dict[str, Any],
    *,
    name: str = "правило",
    priority: int = 10,
) -> dict[str, Any]:
    response = await client.post(
        "/v1/routing-rules",
        json={
            "name": name,
            "priority": priority,
            "conditions": conditions,
            "actions": actions,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def fake(carrier_setup: tuple[Any, Any]) -> FakeCarrier:
    """Зарегистрированный поддельный перевозчик, у которого видно, спрашивали ли его."""
    adapter = FakeCarrier("fake")
    registry.register(adapter)
    return adapter


class TestWriteValidation:
    async def test_an_unknown_condition_key_is_refused(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        """Опечатка не сохраняется, чтобы потом молча запретить всех.

        ``carier`` вместо ``carrier`` при разборе «по известным ключам» дал бы
        пустое условие, то есть правило, совпадающее с любым перевозчиком.
        """
        response = await client.post(
            "/v1/routing-rules",
            json={
                "name": "опечатка",
                "priority": 1,
                "conditions": {"carier": ["fake"]},
                "actions": {"deny": True},
            },
            headers=headers,
        )
        assert response.status_code == 422, response.text

    async def test_a_rule_without_an_action_is_refused(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/v1/routing-rules",
            json={"name": "пустое", "priority": 1, "conditions": {}, "actions": {}},
            headers=headers,
        )
        assert response.status_code == 422, response.text

    async def test_a_taken_priority_is_named(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        """Человек должен прочитать «занят правилом таким-то», а не отказ базы."""
        await _rule(client, headers, {}, {"deny": True}, name="первое", priority=5)
        response = await client.post(
            "/v1/routing-rules",
            json={
                "name": "второе",
                "priority": 5,
                "conditions": {},
                "actions": {"allow": True},
            },
            headers=headers,
        )
        assert response.status_code == 409, response.text
        assert "первое" in response.json()["error"]["message"]

    async def test_conditions_and_actions_change_only_together(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        """Половина правила почти наверняка означает не то, что имел в виду человек."""
        rule = await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True})
        response = await client.patch(
            f"/v1/routing-rules/{rule['id']}",
            json={"actions": {"allow": True}},
            headers=headers,
        )
        assert response.status_code == 422, response.text


class TestPolicyReachesTheQuote:
    async def test_a_denied_carrier_is_not_asked_and_does_not_disappear(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        """Главное следствие ADR-0028: запрет действует ДО опроса.

        Перевозчика не спрашивают — вызов стоит денег, времени и квоты, —
        но строка остаётся: спрятать её значило бы показать выдачу,
        в которой платформа «ничего не нашла», умолчав, что нашла.
        """
        await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True}, name="не возим")

        body = await _rate(client, headers)

        assert fake.seen == [], "запрещённого перевозчика опросили"
        assert body["offers"] == []
        assert body["failures"] == [], "запрет — это не отказ перевозчика"
        assert [b["carrier_code"] for b in body["blocked"]] == ["fake"]
        assert body["blocked"][0]["reason"] == "carrier_blacklisted"
        assert "не возим" in body["blocked"][0]["message"]

    async def test_a_rule_that_does_not_match_leaves_the_quote_alone(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        """Эталонный запрос едет во Владивосток, а правило про Москву."""
        await _rule(
            client,
            headers,
            {"carrier": ["fake"], "direction": {"to": [VLADIVOSTOK]}},
            {"deny": True},
        )
        body = await _rate(client, headers)
        assert body["blocked"] == []
        assert body["offers"]

    async def test_the_direction_of_the_reference_request_is_matched(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        """Тот же запрос, но правило названо в его настоящем направлении."""
        await _rule(
            client,
            headers,
            {"carrier": ["fake"], "direction": {"from": [VLADIVOSTOK], "to": [MOSCOW]}},
            {"deny": True},
            name="Владивосток → Москва не возим",
        )
        body = await _rate(client, headers)
        assert [b["carrier_code"] for b in body["blocked"]] == ["fake"]
        assert body["blocked"][0]["reason"] == "tenant_policy"

    async def test_mandatory_insurance_blocks_a_carrier_that_cannot_insure(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        """Обещать страховку тому, у кого её нет, хуже, чем не показать вариант.

        Эталонный запрос объявляет груз на 480 000 ₽, порог правила — 100 000 ₽.
        """
        await _rule(
            client,
            headers,
            {"cargo_value": {"min_minor": 10_000_000, "currency": "RUB"}},
            {"require_insurance": True},
            name="дорогое страхуем",
        )
        body = await _rate(client, headers)
        assert fake.seen == []
        assert [b["carrier_code"] for b in body["blocked"]] == ["fake"]
        assert "страхован" in body["blocked"][0]["message"]

    async def test_a_whitelist_that_names_nobody_available_blocks_everyone(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        """Whitelist — это «кроме этого — ничего», в том числе когда «этого» нет."""
        await _rule(client, headers, {"carrier": ["pochta"]}, {"allow": True}, name="только Почтой")
        body = await _rate(client, headers)
        assert [b["reason"] for b in body["blocked"]] == ["not_in_whitelist"]

    async def test_a_disabled_rule_does_nothing(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        rule = await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True})
        patched = await client.patch(
            f"/v1/routing-rules/{rule['id']}", json={"enabled": False}, headers=headers
        )
        assert patched.status_code == 200, patched.text

        body = await _rate(client, headers)
        assert body["blocked"] == []
        assert body["offers"]


class TestPolicyVersion:
    async def test_a_new_rule_invalidates_the_reusable_quote(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        """Иначе пятнадцать минут после запрета запрещённый показывался бы с ценой.

        Повтор выдачи (FR-1.6) отпечатком запроса не отличал бы «до правила»
        от «после», и запрет вступал бы в силу с задержкой в срок жизни выдачи.
        """
        first = await _rate(client, headers)
        assert first["offers"]

        await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True})

        second = await _rate(client, headers)
        assert second["quote_id"] != first["quote_id"], "вернулась выдача прежней политики"
        assert second["offers"] == []

    async def test_changing_any_rule_changes_the_version(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        """Дефект, который ADR-0028 называет в §6.

        Версия бралась от правила с наибольшим приоритетом, и правка любого
        другого её не меняла: два разных набора правил давали одинаковую
        версию, и по снимку нельзя было понять, по каким правилам принято
        решение.
        """
        low = await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True}, priority=1)
        await _rule(client, headers, {}, {"auto_select": "cheapest"}, priority=99)

        before = (await client.get("/v1/routing-rules", headers=headers)).json()["policy_version"]

        patched = await client.patch(
            f"/v1/routing-rules/{low['id']}",
            json={"conditions": {"carrier": ["pochta"]}, "actions": {"deny": True}},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text

        after = (await client.get("/v1/routing-rules", headers=headers)).json()["policy_version"]
        assert after != before

    async def test_the_stored_column_matches_the_computed_version(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        seeded_tenants: tuple[Any, Any],
        database_url: str,
    ) -> None:
        """Колонка ``policy_version`` — материализация, а не второй источник истины.

        Рекомендация считает отпечаток от самого набора правил, а колонка
        хранит его же. Разойдись они, это должен увидеть тест, а не аналитик
        через полгода.

        Проверяется и то, что версия записана ВО ВСЕ правила: правило,
        сохранившее прежнюю, утверждало бы, что политика не менялась.
        """
        await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True}, priority=1)
        await _rule(client, headers, {}, {"require_insurance": True}, priority=2)

        computed = (await client.get("/v1/routing-rules", headers=headers)).json()["policy_version"]
        assert computed.startswith("policy-")

        tenant_a, _ = seeded_tenants
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)}
                )
                stored = (
                    await conn.execute(
                        text(
                            "SELECT policy_version FROM routing_rules"
                            " WHERE tenant_id = :t ORDER BY priority"
                        ),
                        {"t": str(tenant_a)},
                    )
                ).scalars()
                assert list(stored) == [computed, computed]
        finally:
            await engine.dispose()

    async def test_the_recommendation_carries_the_current_policy(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        """Версия политики попадает в снимок рекомендации."""
        await _rule(client, headers, {}, {"require_insurance": True}, name="страховать всё")
        listing = (await client.get("/v1/routing-rules", headers=headers)).json()

        # Правило требует страхования, а поддельный перевозчик его не умеет:
        # выдача пуста, но рекомендация всё равно строится — и несёт версию.
        body = await _rate(client, headers)
        response = await client.post(
            "/v1/routing/quote",
            json={"quote_id": body["quote_id"], "strategy": "optimal"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["policy_version"] == listing["policy_version"]


class TestAccess:
    async def test_another_tenant_gets_a_404_and_not_a_403(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        """Наличие объекта у соседа — не то, что стоит подтверждать."""
        rule = await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True})

        from tests.conftest import login

        other = await login(client, "b@example.com")
        response = await client.patch(
            f"/v1/routing-rules/{rule['id']}", json={"enabled": False}, headers=other
        )
        assert response.status_code == 404, response.text

    async def test_a_deleted_rule_stops_acting(
        self, client: AsyncClient, headers: dict[str, str], fake: FakeCarrier
    ) -> None:
        rule = await _rule(client, headers, {"carrier": ["fake"]}, {"deny": True})
        deleted = await client.delete(f"/v1/routing-rules/{rule['id']}", headers=headers)
        assert deleted.status_code == 204, deleted.text

        body = await _rate(client, headers)
        assert body["blocked"] == []
        assert body["offers"]
