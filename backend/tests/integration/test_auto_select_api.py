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
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aerogram.carriers import registry
from tests.integration.conftest import RATE_REQUEST, FakeCarrier

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auto_select_on() -> Iterator[None]:
    """Включить рубильник до сборки приложения.

    Фикстура запрашивается ПЕРВОЙ в сигнатуре теста: ``app`` читает настройки
    при сборке и сбрасывает их кэш, поэтому переменная должна стоять раньше.
    """
    previous = os.environ.get("AUTO_SELECT_ENABLED")
    os.environ["AUTO_SELECT_ENABLED"] = "true"
    yield
    if previous is None:
        os.environ.pop("AUTO_SELECT_ENABLED", None)
    else:
        os.environ["AUTO_SELECT_ENABLED"] = previous


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
