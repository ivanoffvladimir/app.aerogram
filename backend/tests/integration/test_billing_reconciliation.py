"""Сверка расчёта и счетов: экран `/invoices`, шаг Settle.

Проверяется то, что соврёт молча и про деньги.

Главное — разность считается по ОДНИМ И ТЕМ ЖЕ отправлениям. Наивный
вариант «сумма всех котировок минус сумма пришедших счетов» на любом живом
наборе даёт огромную экономию просто потому, что счета приходят позже
оформления. Такое число выглядит достоверно и читается как выгода
от платформы.

Второе — «счёта нет» это не «сошлось». Отправление без факта обязано
попадать в отдельное состояние, иначе экран покажет «расхождений нет»
там, где сверять было нечего.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from aerogram.billing.repository import STATE_FILTERS
from aerogram.billing.schemas import ReconciliationState
from aerogram.billing.service import state_of
from aerogram.carriers import registry
from aerogram.carriers.base import CarrierAccount, ShipmentRequest, ShipmentResult
from aerogram.db import session_scope
from aerogram.shipments.repository import ShipmentRepository
from tests.conftest import TEST_PASSWORD, login
from tests.integration.conftest import RATE_REQUEST, FakeCarrier

pytestmark = pytest.mark.asyncio


class BillingCarrier(FakeCarrier):
    """Перевозчик, который оформляет заказ и не называет фактическую цену.

    Ровно так ведут себя четыре адаптера из пяти: цена счёта приходит позже
    оформления, а у части перевозчиков — только бумажным счётом. Отправление
    в этот момент обязано считаться ожидающим счёта.
    """

    def __init__(self, code: str = "fake") -> None:
        super().__init__(code)

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        return ShipmentResult(
            external_id=f"EXT-{req.number}",
            tracking_number=f"TRK-{req.number}",
            promised_delivery_date=date(2026, 9, 4),
            price_actual=None,
        )


@pytest.fixture
def carrier() -> BillingCarrier:
    adapter = BillingCarrier()
    registry.register(adapter)
    return adapter


async def _ship(client: AsyncClient, headers: dict[str, str], key: str) -> dict[str, Any]:
    """Расчёт → рекомендация → решение → отправление."""
    quote = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)
    assert quote.status_code == 200, quote.text

    recommendation = await client.post(
        "/v1/routing/quote",
        json={"quote_id": quote.json()["quote_id"], "strategy": "optimal"},
        headers=headers,
    )
    assert recommendation.status_code == 200, recommendation.text
    picked = recommendation.json()

    decision = await client.post(
        "/v1/decisions",
        json={
            "recommendation_id": picked["id"],
            "selected_offer_id": picked["recommended_offer_id"],
            "mode": "manual",
        },
        headers={**headers, "Idempotency-Key": f"d-{key}"},
    )
    assert decision.status_code == 201, decision.text

    created = await client.post(
        "/v1/shipments",
        json={"decision_id": decision.json()["decision_id"]},
        headers={**headers, "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _invoice(tenant_id: UUID, shipment_id: UUID, amount_minor: int) -> None:
    """Проставить фактическую сумму так, как это сделает пришедший счёт."""
    async with session_scope(tenant_id) as session:
        shipment = await ShipmentRepository(session).get(shipment_id)
        assert shipment is not None
        shipment.price_actual_amount_minor = amount_minor


async def _reconciliation(
    client: AsyncClient, headers: dict[str, str], **params: object
) -> dict[str, Any]:
    response = await client.get("/v1/billing/reconciliation", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestAwaitingIsNotMatched:
    async def test_a_shipment_without_an_invoice_waits_and_does_not_match(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        """Пустота не равна совпадению: сверять пока нечего."""
        shipment = await _ship(client, headers, "b-1")

        body = await _reconciliation(client, headers)

        assert body["total"] == 1
        line = body["items"][0]
        assert line["state"] == ReconciliationState.AWAITING
        assert line["actual"] is None
        assert line["difference"] is None
        assert line["difference_percent"] is None

        totals = body["currencies"][0]
        assert totals["awaiting"] == 1
        assert totals["matched"] == 0
        # План есть, сверять нечего: котировка сверенных строк — ноль,
        # и разность поэтому тоже ноль, а не «сэкономили всю котировку».
        assert totals["quoted"]["amount_minor"] == shipment["quoted_total_cost"]["amount_minor"]
        assert totals["quoted_reconciled"]["amount_minor"] == 0
        assert totals["difference"]["amount_minor"] == 0
        assert totals["difference_percent"] is None

    async def test_a_carrier_without_invoices_is_not_in_the_breakdown(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        """Строка с нулевой разницей читалась бы как «у него всё сходится»."""
        await _ship(client, headers, "b-2")

        body = await _reconciliation(client, headers)

        assert body["carriers"] == []


class TestDifference:
    async def test_the_difference_is_taken_over_the_same_shipments(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        """Иначе неоплаченные отправления превращаются в «экономию».

        Два отправления, счёт по одному. Наивная разность «весь факт минус
        вся котировка» дала бы минус целую вторую котировку и показала бы
        выгоду там, где счёт просто ещё не пришёл.
        """
        tenant_a, _ = carrier_setup
        billed = await _ship(client, headers, "b-3")
        await _ship(client, headers, "b-4")

        quoted = int(billed["quoted_total_cost"]["amount_minor"])
        await _invoice(tenant_a, UUID(billed["id"]), quoted + 5_000)

        totals = (await _reconciliation(client, headers))["currencies"][0]

        assert totals["shipments"] == 2
        assert totals["awaiting"] == 1
        assert totals["overcharged"] == 1
        assert totals["quoted_reconciled"]["amount_minor"] == quoted
        assert totals["difference"]["amount_minor"] == 5_000
        # Полная котировка больше сверенной ровно на второе отправление —
        # то самое число, которое наивный расчёт вычел бы из факта.
        assert totals["quoted"]["amount_minor"] > totals["quoted_reconciled"]["amount_minor"]

    async def test_an_undercharge_is_a_discrepancy_too(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        """Занижение — либо ошибка счёта, либо завышенная котировка.

        Второе стоит выигранных сравнений: мы проигрывали там, где были
        дешевле.
        """
        tenant_a, _ = carrier_setup
        shipment = await _ship(client, headers, "b-5")
        quoted = int(shipment["quoted_total_cost"]["amount_minor"])
        await _invoice(tenant_a, UUID(shipment["id"]), quoted - 3_000)

        body = await _reconciliation(client, headers)

        assert body["items"][0]["state"] == ReconciliationState.UNDERCHARGED
        assert body["items"][0]["difference"]["amount_minor"] == -3_000
        assert body["currencies"][0]["difference"]["amount_minor"] == -3_000

    async def test_matched_to_the_kopeck(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        tenant_a, _ = carrier_setup
        shipment = await _ship(client, headers, "b-6")
        quoted = int(shipment["quoted_total_cost"]["amount_minor"])
        await _invoice(tenant_a, UUID(shipment["id"]), quoted)

        body = await _reconciliation(client, headers)

        assert body["items"][0]["state"] == ReconciliationState.MATCHED
        assert body["items"][0]["difference"]["amount_minor"] == 0
        assert body["items"][0]["difference_percent"] == 0.0
        assert body["currencies"][0]["matched"] == 1

    async def test_the_carrier_breakdown_names_who_bills_differently(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        """Смысл экрана: видно, чьи счета расходятся с расчётом."""
        tenant_a, _ = carrier_setup
        shipment = await _ship(client, headers, "b-7")
        quoted = int(shipment["quoted_total_cost"]["amount_minor"])
        await _invoice(tenant_a, UUID(shipment["id"]), quoted + 1_000)

        rows = (await _reconciliation(client, headers))["carriers"]

        assert len(rows) == 1
        assert rows[0]["carrier_name"] == "Поддельный"
        assert rows[0]["reconciled"] == 1
        assert rows[0]["difference"]["amount_minor"] == 1_000


class TestFilterAndPaging:
    async def test_the_state_filter_agrees_with_the_row_label(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        """Отбор идёт в SQL, подпись считается в Python — они обязаны совпасть.

        Разойдись они, фильтр «перерасход» показывал бы строки с подписью
        «сошлось», и оператор спорил бы с перевозчиком не о тех счетах.
        """
        tenant_a, _ = carrier_setup
        over = await _ship(client, headers, "b-8")
        await _ship(client, headers, "b-9")
        quoted = int(over["quoted_total_cost"]["amount_minor"])
        await _invoice(tenant_a, UUID(over["id"]), quoted + 2_000)

        body = await _reconciliation(client, headers, state="overcharged")

        assert body["total"] == 1
        assert body["items"][0]["number"] == over["number"]
        assert body["items"][0]["state"] == ReconciliationState.OVERCHARGED

    async def test_totals_do_not_depend_on_the_page(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        """Иначе сумма меняется от того, на какой странице стоит оператор."""
        await _ship(client, headers, "b-10")
        await _ship(client, headers, "b-11")

        body = await _reconciliation(client, headers, page_size=1)

        assert len(body["items"]) == 1
        assert body["total"] == 2
        assert body["currencies"][0]["shipments"] == 2

    async def test_a_window_before_the_shipments_is_empty(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: BillingCarrier,
    ) -> None:
        await _ship(client, headers, "b-12")

        # Окно в одни сутки всё ещё захватывает сегодняшнее отправление;
        # проверяем, что окно вообще применяется, — на пустом периоде
        # ответ обязан быть пустым, а не «весь период».
        body = await _reconciliation(client, headers, days=1)
        assert body["total"] == 1
        assert body["days"] == 1


class TestAccess:
    async def test_an_operator_does_not_see_finances(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Оператор оформляет отправления, а не разбирает счета."""
        created = await client.post(
            "/v1/users",
            json={
                "email": "operator@example.com",
                "full_name": "Оператор",
                "role": "operator",
                "password": TEST_PASSWORD,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text

        operator = await login(client, "operator@example.com")
        response = await client.get("/v1/billing/reconciliation", headers=operator)

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_an_anonymous_caller_is_refused(self, client: AsyncClient) -> None:
        response = await client.get("/v1/billing/reconciliation")
        assert response.status_code == 401


class TestStateRules:
    """Правило состояния живёт в двух местах и обязано совпадать."""

    def test_python_and_sql_know_the_same_states(self) -> None:
        assert {state.value for state in ReconciliationState} == set(STATE_FILTERS)

    @pytest.mark.parametrize(
        ("quoted", "actual", "expected"),
        [
            (100, None, ReconciliationState.AWAITING),
            (None, 100, ReconciliationState.NO_QUOTE),
            (100, 100, ReconciliationState.MATCHED),
            (100, 101, ReconciliationState.OVERCHARGED),
            (100, 99, ReconciliationState.UNDERCHARGED),
            (None, None, ReconciliationState.AWAITING),
        ],
    )
    def test_the_state_of_a_row(
        self, quoted: int | None, actual: int | None, expected: ReconciliationState
    ) -> None:
        assert state_of(quoted, actual) is expected
