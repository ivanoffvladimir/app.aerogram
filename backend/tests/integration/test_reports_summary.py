"""Сводка кабинета: числа, по которым принимают решения о перевозчиках.

Проверяется то, что ошибётся молча. Доля «в срок» считается по доставкам,
у которых дедлайн вообще был, — иначе она разбавляется теми, кого никто
не мерил, и падает без всякой на то причины. Пустое окно даёт ``null``,
а не ноль: по нулю начинают менять перевозчика, по ``null`` — ищут данные.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient

from aerogram.config import get_settings
from aerogram.db import session_scope
from aerogram.shipments.repository import ShipmentRepository
from aerogram.shipments.service import ShipmentService
from tests.integration.conftest import (
    DEADLINE,
    RATE_REQUEST,
    RATE_REQUEST_WITH_DEADLINE,
    TrackingCarrier,
    event,
)

pytestmark = pytest.mark.asyncio

IN_TIME = DEADLINE - timedelta(days=1)
TOO_LATE = DEADLINE + timedelta(hours=6)


async def _decide(
    client: AsyncClient,
    headers: dict[str, str],
    key: str,
    *,
    with_deadline: bool = True,
    override_reason: str | None = None,
) -> str:
    """Расчёт → рекомендация → решение. Возвращает ``decision_id``."""
    payload = RATE_REQUEST_WITH_DEADLINE if with_deadline else RATE_REQUEST
    quote = await client.post("/v1/rates", json=payload, headers=headers)
    assert quote.status_code == 200, quote.text

    recommendation = await client.post(
        "/v1/routing/quote",
        json={"quote_id": quote.json()["quote_id"], "strategy": "optimal"},
        headers=headers,
    )
    assert recommendation.status_code == 200, recommendation.text
    picked = recommendation.json()

    selected = picked["recommended_offer_id"]
    body: dict[str, object] = {
        "recommendation_id": picked["id"],
        "selected_offer_id": selected,
        "mode": "manual",
    }
    if override_reason is not None:
        # Отказ от рекомендации определяется тем, ЧТО выбрали, а не флагом
        # в запросе: иначе метрику можно было бы нарисовать со стороны клиента.
        other = next(o for o in quote.json()["offers"] if o["id"] != selected)
        body["selected_offer_id"] = other["id"]
        body["override"] = True
        body["override_reason"] = override_reason

    decision = await client.post(
        "/v1/decisions", json=body, headers={**headers, "Idempotency-Key": f"d-{key}"}
    )
    assert decision.status_code == 201, decision.text
    return str(decision.json()["decision_id"])


async def _ship(client: AsyncClient, headers: dict[str, str], decision_id: str, key: str) -> dict:
    created = await client.post(
        "/v1/shipments",
        json={"decision_id": decision_id},
        headers={**headers, "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _deliver(
    tenant_id: UUID, shipment_id: UUID, carrier: TrackingCarrier, at: datetime
) -> None:
    """Довести отправление до доставки так, как это сделает опрос."""
    carrier.events = [event("DELIVERED", at=at)]
    async with session_scope(tenant_id) as session:
        shipment = await ShipmentRepository(session).get(shipment_id)
        assert shipment is not None
        await ShipmentService(session, get_settings()).poll(shipment)


class TestOnTime:
    async def test_an_empty_window_reports_null_not_zero(
        self, client: AsyncClient, headers: dict[str, str], cdek_setup: UUID
    ) -> None:
        """Ноль читался бы как «ни одной вовремя» — это разные новости."""
        response = await client.get("/v1/reports/summary", headers=headers)

        body = response.json()
        assert response.status_code == 200, response.text
        assert body["delivery"]["on_time_rate"] is None
        assert body["delivery"]["delivered"] == 0
        assert body["overrides"]["override_rate"] is None

    async def test_a_delivery_in_time_counts_as_on_time(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        decision = await _decide(client, headers, "s-1")
        shipment = await _ship(client, headers, decision, "s-1")
        await _deliver(cdek_setup, UUID(shipment["id"]), carrier, IN_TIME)

        body = (await client.get("/v1/reports/summary", headers=headers)).json()

        assert body["delivery"]["delivered"] == 1
        assert body["delivery"]["on_time"] == 1
        assert body["delivery"]["on_time_rate"] == 100.0
        assert body["delivery"]["average_delay_hours"] is None

    async def test_a_late_delivery_carries_its_delay(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Средняя просрочка считается по опоздавшим, а не по всем.

        Усреднение по всем превращает редкое тяжёлое опоздание в незаметные
        минуты, и по такому числу никто ничего не заметит.
        """
        decision = await _decide(client, headers, "s-2")
        shipment = await _ship(client, headers, decision, "s-2")
        await _deliver(cdek_setup, UUID(shipment["id"]), carrier, TOO_LATE)

        body = (await client.get("/v1/reports/summary", headers=headers)).json()

        assert body["delivery"]["late"] == 1
        assert body["delivery"]["on_time_rate"] == 0.0
        assert body["delivery"]["average_delay_hours"] == 6.0
        assert body["delivery"]["max_delay_hours"] == 6.0

    async def test_a_delivery_without_a_deadline_is_not_in_the_denominator(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Её нельзя ни зачесть, ни засчитать в опоздания."""
        with_deadline = await _decide(client, headers, "s-3")
        first = await _ship(client, headers, with_deadline, "s-3")
        await _deliver(cdek_setup, UUID(first["id"]), carrier, IN_TIME)

        without = await _decide(client, headers, "s-4", with_deadline=False)
        second = await _ship(client, headers, without, "s-4")
        await _deliver(cdek_setup, UUID(second["id"]), carrier, IN_TIME)

        body = (await client.get("/v1/reports/summary", headers=headers)).json()

        assert body["delivery"]["delivered"] == 2
        assert body["delivery"]["with_deadline"] == 1
        # Была бы вторая доставка в знаменателе — доля упала бы до 50 %
        # без единого опоздания.
        assert body["delivery"]["on_time_rate"] == 100.0


class TestCosts:
    async def test_costs_are_grouped_by_currency(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Складывать разные валюты запрещено (CLAUDE.md §6)."""
        decision = await _decide(client, headers, "c-1")
        shipment = await _ship(client, headers, decision, "c-1")

        body = (await client.get("/v1/reports/summary", headers=headers)).json()

        assert len(body["costs"]) == 1
        row = body["costs"][0]
        assert row["currency"] == shipment["quoted_total_cost"]["currency"]
        assert row["shipments"] == 1
        assert row["quoted"]["amount_minor"] == shipment["quoted_total_cost"]["amount_minor"]
        # Счёт ещё не приходил, и это видно отдельным числом: иначе нулевой
        # факт выглядел бы экономией.
        assert row["with_actual"] == 0
        assert row["actual"]["amount_minor"] == 0


class TestOverrides:
    async def test_override_rate_counts_refusals_of_the_recommendation(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Метрика раскладывается по причинам — иначе она бесполезна."""
        # Без дедлайна: он оставил бы проходящим только один вариант,
        # и отказаться от рекомендации было бы не в пользу чего.
        await _decide(client, headers, "o-1", with_deadline=False)
        await _decide(client, headers, "o-2", with_deadline=False, override_reason="cheaper")

        body = (await client.get("/v1/reports/summary", headers=headers)).json()

        assert body["overrides"]["decisions"] == 2
        assert body["overrides"]["overrides"] == 1
        assert body["overrides"]["override_rate"] == 50.0
        assert body["overrides"]["by_reason"] == {"cheaper": 1}


class TestWindow:
    async def test_the_window_is_bounded_and_reported_back(
        self, client: AsyncClient, headers: dict[str, str], cdek_setup: UUID
    ) -> None:
        """Окно возвращается в ответе: по нему читаются все числа сводки."""
        body = (await client.get("/v1/reports/summary?days=7", headers=headers)).json()

        assert body["days"] == 7
        assert datetime.fromisoformat(body["since"]) < datetime.now(UTC)

    async def test_an_impossible_window_is_refused_by_the_contract(
        self, client: AsyncClient, headers: dict[str, str], cdek_setup: UUID
    ) -> None:
        assert (await client.get("/v1/reports/summary?days=0", headers=headers)).status_code == 422
        assert (
            await client.get("/v1/reports/summary?days=4000", headers=headers)
        ).status_code == 422


class TestIsolation:
    async def test_another_tenant_sees_its_own_zeroes(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        cdek_setup: UUID,
        carrier: TrackingCarrier,
    ) -> None:
        """Сводка идёт под RLS: чужие доставки в неё не попадают."""
        from tests.conftest import login

        decision = await _decide(client, headers, "i-1")
        shipment = await _ship(client, headers, decision, "i-1")
        await _deliver(cdek_setup, UUID(shipment["id"]), carrier, IN_TIME)

        other = await login(client, "b@example.com")
        body = (await client.get("/v1/reports/summary", headers=other)).json()

        assert body["delivery"]["delivered"] == 0
        assert body["costs"] == []
        assert body["overrides"]["decisions"] == 0
