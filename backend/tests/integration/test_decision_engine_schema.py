"""Инварианты схемы Decision Engine.

Проверяется не «таблица создалась», а то, что схема не даёт записать данные,
которые обесценили бы аналитику: решение без причины override, изменённый
задним числом снимок, чужое предложение, видимое соседнему тенанту.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from aerogram.shared.ids import uuid7

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def conn(database_url: str, clean_db: None) -> AsyncIterator[AsyncConnection]:
    """Соединение под миграционной ролью: RLS действует и на неё (FORCE)."""
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    async with engine.connect() as connection:
        yield connection
        await connection.rollback()
    await engine.dispose()


async def _set_tenant(conn: AsyncConnection, tenant_id: UUID) -> None:
    await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})


async def _seed(conn: AsyncConnection) -> dict[str, Any]:
    """Тенант, пользователь, перевозчик, снимок расчёта, предложение, рекомендация."""
    ids = {k: uuid7() for k in ("tenant", "user", "carrier", "quote", "offer", "rec")}

    await conn.execute(
        text(
            "INSERT INTO tenants (id, name, status, plan, timezone, ranking_weights)"
            " VALUES (:i, :n, 'active', 'pilot', 'Europe/Moscow', '{}'::jsonb)"
        ),
        {"i": ids["tenant"], "n": "Тестовый"},
    )
    await _set_tenant(conn, ids["tenant"])
    await conn.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, password_hash, full_name, role,"
            "  is_active, mfa_enabled)"
            " VALUES (:i, :t, :e, 'x', 'Тест', 'logistician', true, false)"
        ),
        {"i": ids["user"], "t": ids["tenant"], "e": f"u{ids['user'].hex[:8]}@example.com"},
    )
    await conn.execute(
        text(
            "INSERT INTO carriers (id, code, name, is_active, volumetric_divisor)"
            " VALUES (:i, :c, 'Тест', true, 5000)"
        ),
        {"i": ids["carrier"], "c": f"c{ids['carrier'].hex[:6]}"},
    )
    await conn.execute(
        text(
            "INSERT INTO rate_quotes (id, tenant_id, input_snapshot, hash, valid_until)"
            " VALUES (:i, :t, '{}'::jsonb, 'h', now() + interval '15 minutes')"
        ),
        {"i": ids["quote"], "t": ids["tenant"]},
    )
    await conn.execute(
        text(
            "INSERT INTO rate_offers"
            " (id, tenant_id, quote_id, carrier_id, total_amount_minor, currency, valid_until)"
            " VALUES (:i, :t, :q, :c, 245000, 'RUB', now() + interval '15 minutes')"
        ),
        {"i": ids["offer"], "t": ids["tenant"], "q": ids["quote"], "c": ids["carrier"]},
    )
    await conn.execute(
        text(
            "INSERT INTO recommendations"
            " (id, tenant_id, quote_id, recommended_offer_id, strategy, explanation,"
            "  algorithm_version, policy_version)"
            " VALUES (:i, :t, :q, :o, 'optimal', '[]'::jsonb, 'routing-1.0.0', 'tenant-1')"
        ),
        {"i": ids["rec"], "t": ids["tenant"], "q": ids["quote"], "o": ids["offer"]},
    )
    return ids


async def _insert_decision(conn: AsyncConnection, ids: dict[str, Any], **overrides: Any) -> UUID:
    decision_id = uuid7()
    params: dict[str, Any] = {
        "i": decision_id,
        "t": ids["tenant"],
        "r": ids["rec"],
        "o": ids["offer"],
        "a": ids["user"],
        "mode": "manual",
        "ovr": False,
        "reason": None,
        "key": f"k-{decision_id.hex[:12]}",
    }
    params.update(overrides)
    await conn.execute(
        text(
            "INSERT INTO decisions"
            " (id, tenant_id, recommendation_id, selected_offer_id, actor_id, mode,"
            "  override, override_reason, idempotency_key, request_fingerprint)"
            " VALUES (:i, :t, :r, :o, :a, :mode, :ovr, :reason, :key, 'fp')"
        ),
        params,
    )
    return decision_id


class TestDecisionInvariants:
    async def test_override_without_a_reason_is_rejected(self, conn: AsyncConnection) -> None:
        """Override Rate раскладывается по причинам — иначе метрика бесполезна."""
        ids = await _seed(conn)
        with pytest.raises(Exception, match="override_states_the_reason"):
            await _insert_decision(conn, ids, ovr=True, reason=None)

    async def test_override_with_a_reason_is_accepted(self, conn: AsyncConnection) -> None:
        ids = await _seed(conn)
        await _insert_decision(conn, ids, ovr=True, reason="recipient_requirement")

    async def test_manual_decision_without_an_actor_is_rejected(
        self, conn: AsyncConnection
    ) -> None:
        """У ручного решения обязан быть автор: иначе некому задать вопрос."""
        ids = await _seed(conn)
        with pytest.raises(Exception, match="manual_decision_has_an_actor"):
            await _insert_decision(conn, ids, a=None)

    async def test_automatic_decision_needs_no_actor(self, conn: AsyncConnection) -> None:
        ids = await _seed(conn)
        await _insert_decision(conn, ids, mode="auto", a=None)

    async def test_the_same_key_cannot_be_used_twice(self, conn: AsyncConnection) -> None:
        """Идемпотентность: повтор запроса не создаёт второе решение."""
        ids = await _seed(conn)
        await _insert_decision(conn, ids, key="repeated-key")
        with pytest.raises(Exception, match="uq_decisions_tenant_id_idempotency_key"):
            await _insert_decision(conn, ids, key="repeated-key")


class TestDecisionImmutability:
    async def test_selected_offer_cannot_be_changed_afterwards(self, conn: AsyncConnection) -> None:
        """Снимок решения неизменяем (продуктовое ТЗ, раздел 8).

        Проверка на уровне БД, а не приложения: приложение обходится скриптом,
        триггер — нет.
        """
        ids = await _seed(conn)
        decision_id = await _insert_decision(conn, ids)

        other_offer = uuid7()
        await conn.execute(
            text(
                "INSERT INTO rate_offers"
                " (id, tenant_id, quote_id, carrier_id, total_amount_minor, currency, valid_until)"
                " VALUES (:i, :t, :q, :c, 999000, 'RUB', now() + interval '15 minutes')"
            ),
            {"i": other_offer, "t": ids["tenant"], "q": ids["quote"], "c": ids["carrier"]},
        )

        with pytest.raises(Exception, match="неизменяем"):
            await conn.execute(
                text("UPDATE decisions SET selected_offer_id = :o WHERE id = :i"),
                {"o": other_offer, "i": decision_id},
            )

    async def test_comment_can_still_be_corrected(self, conn: AsyncConnection) -> None:
        """Неизменяем снимок решения, а не пояснение к нему.

        Опечатка в комментарии должна правиться: запрет на всё подряд заставил бы
        заводить второе решение ради исправления слова.
        """
        ids = await _seed(conn)
        decision_id = await _insert_decision(conn, ids, ovr=True, reason="other")

        await conn.execute(
            text("UPDATE decisions SET override_comment = :c WHERE id = :i"),
            {"c": "уточнение", "i": decision_id},
        )


class TestOfferInvariants:
    async def test_ineligible_offer_must_state_the_reason(self, conn: AsyncConnection) -> None:
        """Строка без причины выглядит в интерфейсе необъяснимо отключённой."""
        ids = await _seed(conn)
        with pytest.raises(Exception, match="ineligible_offer_states_the_reason"):
            await conn.execute(
                text("UPDATE rate_offers SET eligible = false WHERE id = :i"),
                {"i": ids["offer"]},
            )

    async def test_probability_written_as_a_percent_is_rejected(
        self, conn: AsyncConnection
    ) -> None:
        """Вероятность в процентах вместо доли — типичная ошибка на границе.

        Здесь срабатывает не CHECK, а точность NUMERIC(4,3): 97 не помещается.
        Проверяется именно отказ: важно, что такое значение в базу не попадает.
        """
        ids = await _seed(conn)
        with pytest.raises(Exception, match="numeric field overflow"):
            await conn.execute(
                text("UPDATE rate_offers SET on_time_probability = 97 WHERE id = :i"),
                {"i": ids["offer"]},
            )

    async def test_probability_above_one_is_rejected_by_the_check(
        self, conn: AsyncConnection
    ) -> None:
        """1.5 проходит по точности, но вероятностью не является."""
        ids = await _seed(conn)
        with pytest.raises(Exception, match="on_time_probability_is_a_probability"):
            await conn.execute(
                text("UPDATE rate_offers SET on_time_probability = 1.5 WHERE id = :i"),
                {"i": ids["offer"]},
            )

    async def test_probability_as_a_fraction_is_accepted(self, conn: AsyncConnection) -> None:
        ids = await _seed(conn)
        await conn.execute(
            text("UPDATE rate_offers SET on_time_probability = 0.97 WHERE id = :i"),
            {"i": ids["offer"]},
        )


class TestRoutingRuleInvariants:
    async def test_two_rules_cannot_share_a_priority(self, conn: AsyncConnection) -> None:
        """Иначе порядок применения зависит от порядка чтения строк."""
        ids = await _seed(conn)
        for _ in range(2):
            stmt = text(
                "INSERT INTO routing_rules"
                " (id, tenant_id, name, priority, conditions, actions, policy_version)"
                " VALUES (:i, :t, 'Правило', 10, '{}'::jsonb, '{}'::jsonb, 'v1')"
            )
            if _ == 0:
                await conn.execute(stmt, {"i": uuid7(), "t": ids["tenant"]})
            else:
                with pytest.raises(Exception, match="uq_routing_rules_tenant_id_priority"):
                    await conn.execute(stmt, {"i": uuid7(), "t": ids["tenant"]})


class TestDeliveryOutcomeInvariants:
    async def test_actual_cost_without_a_currency_is_rejected(self, conn: AsyncConnection) -> None:
        """Сумма без валюты не существует (ADR-0011)."""
        ids = await _seed(conn)
        shipment_id = await _insert_shipment(conn, ids)
        with pytest.raises(Exception, match="actual_cost_has_a_currency"):
            await conn.execute(
                text(
                    "INSERT INTO delivery_outcomes"
                    " (shipment_id, tenant_id, actual_amount_minor)"
                    " VALUES (:s, :t, 247500)"
                ),
                {"s": shipment_id, "t": ids["tenant"]},
            )

    async def test_delivery_is_recorded_before_the_invoice_arrives(
        self, conn: AsyncConnection
    ) -> None:
        """Факт доставки не ждёт счёта: SLA считается сразу, стоимость позже."""
        ids = await _seed(conn)
        shipment_id = await _insert_shipment(conn, ids)
        await conn.execute(
            text(
                "INSERT INTO delivery_outcomes"
                " (shipment_id, tenant_id, delivered_at, deadline_met)"
                " VALUES (:s, :t, now(), true)"
            ),
            {"s": shipment_id, "t": ids["tenant"]},
        )

    async def test_one_shipment_cannot_have_two_outcomes(self, conn: AsyncConnection) -> None:
        ids = await _seed(conn)
        shipment_id = await _insert_shipment(conn, ids)
        stmt = text(
            "INSERT INTO delivery_outcomes (shipment_id, tenant_id, deadline_met)"
            " VALUES (:s, :t, true)"
        )
        await conn.execute(stmt, {"s": shipment_id, "t": ids["tenant"]})
        with pytest.raises(Exception, match="pk_delivery_outcomes"):
            await conn.execute(stmt, {"s": shipment_id, "t": ids["tenant"]})


async def _insert_shipment(conn: AsyncConnection, ids: dict[str, Any]) -> UUID:
    shipment_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO shipments (id, tenant_id, carrier_id, number, status, currency,"
            "  payment_type, created_via, has_incident)"
            " VALUES (:i, :t, :c, :n, 'CREATED', 'RUB', 'sender', 'web', false)"
        ),
        {"i": shipment_id, "t": ids["tenant"], "c": ids["carrier"], "n": shipment_id.hex[:12]},
    )
    return shipment_id


class TestTenantIsolation:
    async def test_another_tenant_sees_no_decisions(self, conn: AsyncConnection) -> None:
        """RLS на новых таблицах: без неё они открыты всем тенантам."""
        ids = await _seed(conn)
        await _insert_decision(conn, ids)

        stranger = uuid7()
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, status, plan, timezone, ranking_weights)"
                " VALUES (:i, 'Чужой', 'active', 'pilot', 'Europe/Moscow', '{}'::jsonb)"
            ),
            {"i": stranger},
        )
        await _set_tenant(conn, stranger)

        for table in ("decisions", "recommendations", "rate_offers", "routing_rules"):
            visible = (
                await conn.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            assert visible == 0, f"{table} виден чужому тенанту"
