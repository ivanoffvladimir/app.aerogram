"""Автовыбор доходит до решения: правило создаёт ``Decision`` с ``mode=auto``.

Формулы выбора и формат замороженного вердикта проверены модульно
(``test_selection.py``, ``test_policy_snapshot.py``). Здесь проверяется
то, чего модульный тест увидеть не может: вердикт действительно
замораживается при расчёте, доезжает до рекомендации и превращается
в решение — или честно не превращается, с названной причиной.

Ровно этой цепочки не хватало: правило ``auto_select`` заводилось,
хранилось и показывалось в кабинете, а доля решений без человека могла
быть только нулём (ADR-0029).
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aerogram.carriers import registry
from aerogram.shared.ids import uuid7
from tests.conftest import login
from tests.integration.conftest import RATE_REQUEST, FakeCarrier

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake(carrier_setup: tuple[UUID, UUID]) -> FakeCarrier:
    registry.register(FakeCarrier("fake"))
    return registry.get_adapter("fake")  # type: ignore[return-value]


async def _rule(
    client: AsyncClient, headers: dict[str, str], rule: str, *, name: str = "автовыбор"
) -> dict[str, Any]:
    response = await client.post(
        "/v1/routing-rules",
        json={
            "name": name,
            "priority": 10,
            # Условие по типу груза, а не пустое: правило без условий язык
            # не принимает, а эталонный запрос везёт именно оборудование.
            "conditions": {"cargo_type": ["equipment"]},
            "actions": {"auto_select": rule},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _quote(client: AsyncClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    response = await client.post("/v1/rates", json={**RATE_REQUEST, **overrides}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _recommend(
    client: AsyncClient, headers: dict[str, str], quote_id: str, strategy: str = "optimal"
) -> dict[str, Any]:
    response = await client.post(
        "/v1/routing/quote",
        json={"quote_id": quote_id, "strategy": strategy},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _decisions(database_url: str, tenant_id: UUID) -> list[Any]:
    """Решения тенанта. Читаются напрямую: API их пока не отдаёт.

    Тенант ставится явно: соединение миграций в CI принадлежит
    суперпользователю, и RLS его не ограничивает — запрос без условия
    видел бы строки соседних тестов.
    """
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, mode, actor_id, selected_offer_id, override, override_reason,"
                        " selection_rule, auto_select_rule_id, auto_select_rule_name,"
                        " selection_version, idempotency_key"
                        " FROM decisions WHERE tenant_id = :t ORDER BY decided_at"
                    ),
                    {"t": str(tenant_id)},
                )
            ).all()
    finally:
        await engine.dispose()
    return list(rows)


class TestSwitch:
    async def test_nothing_happens_while_the_switch_is_off(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Правило заведено, рубильник выключен — решений нет.

        Выбор перевозчика без человека включается решением владельца
        площадки, а не выкаткой кода.
        """
        await _rule(client, headers, "cheapest")
        quote = await _quote(client, headers)
        await _recommend(client, headers, quote["quote_id"])

        assert await _decisions(database_url, carrier_setup[0]) == []


class TestAutoDecision:
    async def test_a_rule_decides_without_a_human(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Снимок решения называет правило: значение, идентификатор, имя, версию."""
        rule = await _rule(client, headers, "cheapest", name="берём дешёвое")
        quote = await _quote(client, headers)
        await _recommend(client, headers, quote["quote_id"])

        rows = await _decisions(database_url, carrier_setup[0])
        assert len(rows) == 1
        decision = rows[0]
        assert decision.mode == "auto"
        assert decision.actor_id is None, "у машинного решения нет автора"
        assert decision.selection_rule == "cheapest"
        assert str(decision.auto_select_rule_id) == rule["id"]
        assert decision.auto_select_rule_name == "берём дешёвое"
        assert decision.selection_version == "selection-1.0.0"
        assert decision.idempotency_key == f"auto:{quote['quote_id']}"

        cheapest = min(
            (o for o in quote["offers"] if o.get("total_cost")),
            key=lambda o: o["total_cost"]["amount_minor"],
        )
        assert str(decision.selected_offer_id) == cheapest["id"]

    async def test_disagreeing_with_the_recommendation_is_an_override_with_its_own_reason(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Дешёвое против быстрого: словари правила и стратегии не пересекаются.

        Расхождение — нормальный исход, и записывается оно честно: причина
        ``auto_select_rule``, а не ``corporate_policy`` (мотив человека).
        """
        await _rule(client, headers, "cheapest")
        quote = await _quote(client, headers, strategy="fastest")
        recommendation = await _recommend(client, headers, quote["quote_id"], strategy="fastest")

        rows = await _decisions(database_url, carrier_setup[0])
        assert len(rows) == 1
        decision = rows[0]
        assert str(decision.selected_offer_id) != recommendation["recommended_offer_id"]
        assert decision.override is True
        assert decision.override_reason == "auto_select_rule"

    async def test_a_second_recommendation_does_not_decide_twice(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Ключ идемпотентности — от расчёта, а не от рекомендации.

        Кабинет зовёт рекомендацию при каждой смене вкладки стратегии,
        и каждый вызов создаёт НОВУЮ рекомендацию. Ключ от неё дал бы
        по решению на вкладку.
        """
        await _rule(client, headers, "cheapest")
        quote = await _quote(client, headers)
        first = await _recommend(client, headers, quote["quote_id"])
        second = await _recommend(client, headers, quote["quote_id"])

        assert first["id"] != second["id"], "рекомендация не идемпотентна — на этом и держится тест"
        assert len(await _decisions(database_url, carrier_setup[0])) == 1

    async def test_another_strategy_does_not_trigger_the_rule(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Гейт по стратегии: иначе ``override`` зависел бы от вкладки.

        Расчёт запрошен со стратегией ``optimal``; рекомендация по вкладке
        ``cheapest`` автовыбор не запускает.
        """
        await _rule(client, headers, "cheapest")
        quote = await _quote(client, headers)
        await _recommend(client, headers, quote["quote_id"], strategy="cheapest")

        assert await _decisions(database_url, carrier_setup[0]) == []

    async def test_without_a_rule_nothing_is_decided(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        quote = await _quote(client, headers)
        await _recommend(client, headers, quote["quote_id"])

        assert await _decisions(database_url, carrier_setup[0]) == []

    async def test_a_rule_that_cannot_choose_abstains(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Скора у нового перевозчика нет, и «выбрать хоть что-то» — не выход.

        Это и была бы скрытая автоматизация: правило ``best_score``
        исполнилось бы как «самый дешёвый», то есть не то правило,
        которое написал человек.
        """
        await _rule(client, headers, "best_score")
        quote = await _quote(client, headers)
        await _recommend(client, headers, quote["quote_id"])

        assert await _decisions(database_url, carrier_setup[0]) == []


class TestRecommendationCarriesTheDecision:
    """Экран обязан узнать о машинном выборе вместе с рекомендацией.

    Иначе оператор нажмёт «Принять рекомендацию» и создаст по тому же
    расчёту второе решение, не зная о первом.
    """

    async def test_the_recommendation_names_the_rule_that_already_chose(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        rule = await _rule(client, headers, "cheapest", name="берём дешёвое")
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])

        auto = recommendation["auto_decision"]
        assert auto is not None
        assert auto["rule"] == "cheapest"
        assert auto["rule_id"] == rule["id"]
        assert auto["rule_name"] == "берём дешёвое"
        assert auto["selection_version"] == "selection-1.0.0"
        assert auto["selected_offer_id"] in {o["id"] for o in quote["offers"]}

    async def test_a_reopened_screen_still_sees_it(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Вторая рекомендация по тому же расчёту — это обновлённый экран.

        Решение уже принято и не создаётся заново, но молчать о нём нельзя:
        экран показал бы кнопку выбора там, где выбор уже сделан.
        """
        await _rule(client, headers, "cheapest")
        quote = await _quote(client, headers)
        first = await _recommend(client, headers, quote["quote_id"])
        second = await _recommend(client, headers, quote["quote_id"])

        assert second["auto_decision"] is not None
        assert second["auto_decision"]["decision_id"] == first["auto_decision"]["decision_id"]

    async def test_without_a_rule_the_field_is_empty(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Выбор остаётся за человеком, и экран не должен думать иначе."""
        quote = await _quote(client, headers)
        assert (await _recommend(client, headers, quote["quote_id"]))["auto_decision"] is None


class TestReadDecision:
    """``GET /v1/decisions/{id}`` — чем объясняется выбор."""

    async def test_an_automatic_decision_explains_itself(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        rule = await _rule(client, headers, "cheapest", name="берём дешёвое")
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        decision_id = recommendation["auto_decision"]["decision_id"]

        response = await client.get(f"/v1/decisions/{decision_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "auto"
        assert body["actor_id"] is None
        assert body["quote_id"] == quote["quote_id"]
        assert body["recommendation_id"] == recommendation["id"]
        assert body["selection_rule"] == "cheapest"
        assert body["auto_select_rule_id"] == rule["id"]
        assert body["auto_select_rule_name"] == "берём дешёвое"
        assert body["selection_version"] == "selection-1.0.0"

    async def test_a_manual_decision_names_no_rule(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Пустой снимок автовыбора — утверждение «выбрал человек»."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        created = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
                "mode": "manual",
            },
            headers={**headers, "Idempotency-Key": "manual-1"},
        )
        assert created.status_code == 201, created.text

        body = (
            await client.get(f"/v1/decisions/{created.json()['decision_id']}", headers=headers)
        ).json()
        assert body["mode"] == "manual"
        assert body["actor_id"] is not None
        assert body["selection_rule"] is None
        assert body["auto_select_rule_id"] is None

    async def test_a_foreign_decision_is_a_404_not_a_403(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
        seeded_tenants: tuple[UUID, UUID],
    ) -> None:
        """403 подтвердил бы, что такой объект существует (CLAUDE.md §6)."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        created = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
                "mode": "manual",
            },
            headers={**headers, "Idempotency-Key": "manual-1"},
        )
        assert created.status_code == 201, created.text

        stranger = await login(client, "b@example.com")
        response = await client.get(
            f"/v1/decisions/{created.json()['decision_id']}", headers=stranger
        )
        assert response.status_code == 404, response.text

    async def test_an_unknown_id_is_a_404(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        response = await client.get(f"/v1/decisions/{uuid7()}", headers=headers)
        assert response.status_code == 404, response.text


class TestOverrideRateDenominator:
    """Override Rate меряет доверие ЛЮДЕЙ к движку (ADR-0029).

    Включение одного правила у одного клиента не должно поднимать метрику
    всего пилота, ничего не сказав о логистах.
    """

    async def test_a_rule_decision_does_not_enter_the_denominator(
        self,
        auto_select_on: None,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        await _rule(client, headers, "cheapest")
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        assert recommendation["auto_decision"] is not None

        summary = (await client.get("/v1/reports/summary", headers=headers)).json()
        overrides = summary["overrides"]
        assert overrides["decisions"] == 1, "решение существует и считается общим числом"
        assert overrides["manual"] == 0
        assert overrides["auto_by_rule"] == 1
        assert overrides["auto_by_client"] == 0
        # Ноль читался бы как «люди ни разу не отказались от рекомендации»,
        # хотя людей тут не было вовсе.
        assert overrides["override_rate"] is None
        assert overrides["by_reason"] == {}, "разрез должен сходиться с числителем"


class TestReservedKey:
    async def test_a_client_cannot_claim_the_auto_prefix(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Иначе клиент занял бы ключ будущего решения или подделал машинное."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
                "mode": "manual",
            },
            headers={**headers, "Idempotency-Key": f"auto:{quote['quote_id']}"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["field"] == "Idempotency-Key"

    async def test_a_human_cannot_claim_the_rule_as_a_reason(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Значение заведено, чтобы отличать правило от мотива человека.

        Разреши человеку на него сослаться — и разница стёрлась бы ровно
        там, где заводилась (ADR-0029).
        """
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        other = next(
            offer
            for offer in quote["offers"]
            if offer["id"] != recommendation["recommended_offer_id"]
        )

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": other["id"],
                "override": True,
                "override_reason": "auto_select_rule",
                "mode": "manual",
            },
            headers={**headers, "Idempotency-Key": "pretending"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["field"] == "override_reason"


class TestPolicyVersion:
    async def test_the_recommendation_names_the_policy_of_its_own_quote(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        fake: FakeCarrier,
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Версия берётся из расчёта, а не пересчитывается по нынешним правилам.

        Иначе снимок назвал бы политику, которая этот расчёт не порождала,
        — и историческое решение стало бы необъяснимым.
        """
        quote = await _quote(client, headers)
        await _rule(client, headers, "cheapest", name="появилось после расчёта")
        listing = (await client.get("/v1/routing-rules", headers=headers)).json()

        recommendation = await _recommend(client, headers, quote["quote_id"])

        assert recommendation["policy_version"] != listing["policy_version"]
