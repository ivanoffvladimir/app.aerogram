"""Путь рекомендация → решение по API.

Проверяется то, ради чего существует продукт: рекомендация объяснима,
решение неизменяемо, повтор запроса не создаёт второго решения.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.core.models import ApiKey
from aerogram.core.security import generate_api_key
from aerogram.shared.ids import uuid7
from tests.integration.conftest import RATE_REQUEST, FakeCarrier

pytestmark = pytest.mark.asyncio


async def _quote(client: AsyncClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    # Регистрация однократна: тест может запросить расчёт дважды, а реестр
    # намеренно запрещает переопределять уже зарегистрированный адаптер.
    try:
        registry.get_adapter("fake")
    except LookupError:
        registry.register(FakeCarrier("fake"))
    payload = {**RATE_REQUEST, **overrides}
    response = await client.post("/v1/rates", json=payload, headers=headers)
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


def _decide_headers(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


async def _last_used_at(database_url: str, tenant_id: UUID, prefix: str) -> datetime | None:
    """Когда именно этот ключ использовался в последний раз.

    Ключ выбирается по префиксу, а не «первый попавшийся»: соединение
    миграций в CI принадлежит суперпользователю, RLS его не ограничивает,
    и запрос без условия видел бы ключи, оставшиеся от соседних тестов.
    """
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            row = (
                await conn.execute(
                    text("SELECT last_used_at FROM api_keys WHERE prefix = :p"), {"p": prefix}
                )
            ).one()
    finally:
        await engine.dispose()
    return row.last_used_at


async def _issue_api_key(database_url: str, tenant_id: UUID) -> tuple[str, str]:
    """Выпустить API-ключ тенанта: полное значение и префикс.

    Полное значение существует только здесь, префикс нужен, чтобы найти
    в базе именно этот ключ.
    """
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    full, prefix, key_hash = generate_api_key("local")
    async with factory() as db, db.begin():
        await db.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )
        db.add(
            ApiKey(
                tenant_id=tenant_id,
                name="Тестовый клиент",
                prefix=prefix,
                key_hash=key_hash,
                scopes=["decisions:write", "carriers:read"],
            )
        )
    await engine.dispose()
    return full, prefix


class TestRecommendation:
    async def test_recommends_an_offer_with_an_explanation(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        quote = await _quote(client, headers)
        body = await _recommend(client, headers, quote["quote_id"])

        assert body["recommended_offer_id"] in {o["id"] for o in quote["offers"]}
        assert body["explanation"], "рекомендация без объяснения бесполезна оператору"
        assert body["algorithm_version"] == "routing-1.0.0"
        assert body["policy_version"]

    async def test_cheapest_strategy_picks_the_cheapest(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        quote = await _quote(client, headers)
        body = await _recommend(client, headers, quote["quote_id"], "cheapest")

        cheapest = min(quote["offers"], key=lambda o: o["total_cost"]["amount_minor"])
        assert body["recommended_offer_id"] == cheapest["id"]

    async def test_confidence_is_low_without_delivery_history(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Ни одной доставки ещё не было: изображать уверенность нельзя."""
        quote = await _quote(client, headers)
        body = await _recommend(client, headers, quote["quote_id"])
        assert body["confidence"] == "low"

    async def test_nothing_is_recommended_when_nobody_fits_the_deadline(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        quote = await _quote(client, headers, deadline="2026-09-01T12:00:00+03:00")
        body = await _recommend(client, headers, quote["quote_id"])

        assert body["recommended_offer_id"] is None
        assert body["explanation"] == ["Подходящих вариантов нет"]

    async def test_unknown_quote_gives_404(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        response = await client.post(
            "/v1/routing/quote",
            json={"quote_id": str(uuid7()), "strategy": "optimal"},
            headers=headers,
        )
        assert response.status_code == 404


class TestDecision:
    async def test_accepting_the_recommendation_is_not_an_override(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
            },
            headers=_decide_headers(headers, "decision-1"),
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["snapshot_id"] == quote["quote_id"]

    async def test_choosing_another_offer_requires_a_reason(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Override Rate раскладывается по причинам — иначе метрика бесполезна."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        other = next(
            o for o in quote["offers"] if o["id"] != recommendation["recommended_offer_id"]
        )

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": other["id"],
            },
            headers=_decide_headers(headers, "decision-2"),
        )

        assert response.status_code == 422
        assert response.json()["error"]["field"] == "override_reason"

    async def test_override_with_a_reason_is_recorded(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        other = next(
            o for o in quote["offers"] if o["id"] != recommendation["recommended_offer_id"]
        )

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": other["id"],
                "override": True,
                "override_reason": "recipient_requirement",
                "override_comment": "Получатель работает только с этим перевозчиком",
            },
            headers=_decide_headers(headers, "decision-3"),
        )
        assert response.status_code == 201, response.text

    async def test_an_offer_from_another_quote_is_rejected(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Иначе снимок решения перестал бы объяснять сам себя."""
        first = await _quote(client, headers)
        # Второй запрос отличается по существу, а не по времени: одинаковое
        # тело в пределах срока жизни вернуло бы ту же выдачу (FR-1.6),
        # и «предложение из другой выдачи» проверять было бы нечем.
        second = await _quote(
            client,
            headers,
            packages=[{"weight_grams": 3_000, "length_mm": 200, "width_mm": 150, "height_mm": 100}],
        )
        assert second["quote_id"] != first["quote_id"]
        recommendation = await _recommend(client, headers, first["quote_id"])

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": second["offers"][0]["id"],
                "override": True,
                "override_reason": "other",
            },
            headers=_decide_headers(headers, "decision-4"),
        )

        assert response.status_code == 422
        assert response.json()["error"]["field"] == "selected_offer_id"

    async def test_missing_idempotency_key_is_rejected(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
            },
            headers=headers,
        )
        assert response.status_code == 422


class TestIdempotency:
    async def test_the_same_key_and_body_returns_the_same_decision(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Повтор после обрыва сети не должен создавать второе решение."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        body = {
            "recommendation_id": recommendation["id"],
            "selected_offer_id": recommendation["recommended_offer_id"],
        }
        sent = _decide_headers(headers, "repeat-me")

        first = await client.post("/v1/decisions", json=body, headers=sent)
        second = await client.post("/v1/decisions", json=body, headers=sent)

        assert first.status_code == 201
        assert second.json()["decision_id"] == first.json()["decision_id"]

    async def test_the_same_key_with_a_different_body_gives_409(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Клиент, изменивший тело, ждёт нового действия: молча отдать ему
        прошлый результат хуже, чем честно отказать."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        other = next(
            o for o in quote["offers"] if o["id"] != recommendation["recommended_offer_id"]
        )
        sent = _decide_headers(headers, "same-key")

        await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
            },
            headers=sent,
        )
        conflict = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": other["id"],
                "override": True,
                "override_reason": "cheaper",
            },
            headers=sent,
        )

        assert conflict.status_code == 409
        assert conflict.json()["error"]["field"] == "Idempotency-Key"


class TestMachineClient:
    """Решение без пользователя — путь клиента по API-ключу."""

    async def test_manual_mode_without_a_user_is_a_clear_refusal(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """Раньше такой запрос нарушал ограничение схемы и получал 409.

        У ручного решения обязан быть автор. Клиент по API-ключу человеком
        не является: вместо непонятного «конфликта» он должен получить
        внятное указание, что прислать.
        """
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        tenant_a, _ = carrier_setup
        api_key, _ = await _issue_api_key(database_url, tenant_a)

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
                "mode": "manual",
            },
            headers={"X-Api-Key": api_key, "Idempotency-Key": "machine-1"},
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["field"] == "mode"

    async def test_key_usage_is_recorded(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """``last_used_at`` обязан заполняться при обращении по ключу.

        Раньше отметка ставилась до установки тенанта, и под RLS ``UPDATE``
        молча не находил строк: при компрометации ключа было бы не видно,
        пользовались им или нет.
        """
        tenant_a, _ = carrier_setup
        api_key, prefix = await _issue_api_key(database_url, tenant_a)

        before = await _last_used_at(database_url, tenant_a, prefix)
        assert before is None, "ключ ещё не использовался"

        # Путь выбран внутри области ключа: адресная книга машинному клиенту
        # закрыта целиком (core.scopes), и отказ проверял бы не то.
        response = await client.get("/v1/carriers", headers={"X-Api-Key": api_key})
        assert response.status_code == 200, response.text

        assert await _last_used_at(database_url, tenant_a, prefix) is not None

    async def test_machine_client_records_an_automatic_decision(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        database_url: str,
    ) -> None:
        """С правильным режимом клиент по ключу решение всё-таки фиксирует."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])
        tenant_a, _ = carrier_setup
        api_key, _ = await _issue_api_key(database_url, tenant_a)

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
                "mode": "auto",
            },
            headers={"X-Api-Key": api_key, "Idempotency-Key": "machine-2"},
        )
        assert response.status_code == 201, response.text

    async def test_automatic_decision_records_no_actor(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Автовыбор не требует автора: решение принимает правило."""
        quote = await _quote(client, headers)
        recommendation = await _recommend(client, headers, quote["quote_id"])

        response = await client.post(
            "/v1/decisions",
            json={
                "recommendation_id": recommendation["id"],
                "selected_offer_id": recommendation["recommended_offer_id"],
                "mode": "auto",
            },
            headers=_decide_headers(headers, "auto-1"),
        )
        assert response.status_code == 201, response.text
