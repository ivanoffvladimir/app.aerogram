"""Создание, отмена и сверка «призраков» (FR-2.1 … FR-2.6).

Главное, что здесь проверяется, — второго заказа у перевозчика не возникает
ни при повторе запроса, ни после потерянного ответа. Это единственная ошибка
модуля, которую нельзя исправить задним числом: груз уже поехал.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from aerogram.carriers import registry
from aerogram.carriers.base import (
    CancelResult,
    Capabilities,
    CarrierAccount,
    ShipmentRequest,
    ShipmentResult,
)
from aerogram.db import session_scope
from aerogram.shared.clock import utcnow
from aerogram.shared.errors import CarrierTimeout
from aerogram.shared.money import Money
from aerogram.shipments.repository import ShipmentRepository
from aerogram.shipments.service import shipment_number
from tests.integration.conftest import RATE_REQUEST, FakeCarrier

pytestmark = pytest.mark.asyncio


class ShippingCarrier(FakeCarrier):
    """Поддельный ТК, который умеет ещё и создавать, искать и отменять заказы.

    Считает обращения: без счётчиков «дубля не возникло» проверить нечем —
    ответ клиента выглядит одинаково и когда заказ один, и когда их два.
    """

    def __init__(
        self,
        code: str = "fake",
        *,
        create_behaviour: str = "ok",
        has_order: bool = False,
        cancel_accepted: bool = True,
        supports_cancel: bool = True,
    ) -> None:
        super().__init__(code)
        self.capabilities = Capabilities(supports_cancel=supports_cancel)
        self.create_behaviour = create_behaviour
        #: Есть ли у перевозчика заказ с нашим номером. Так изображается
        #: «призрак»: заказ создан, а ответ до нас не дошёл.
        self.has_order = has_order
        self.cancel_accepted = cancel_accepted
        self.created: list[str] = []
        self.searched: list[str] = []
        self.cancelled: list[str] = []

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        self.created.append(req.number)
        if self.create_behaviour == "timeout":
            # Запрос ушёл, ответ не вернулся: заказ у ТК при этом создан.
            self.has_order = True
            raise CarrierTimeout(carrier_code=self.code)
        return ShipmentResult(
            external_id=f"EXT-{req.number}",
            tracking_number=f"TRK-{req.number}",
            promised_delivery_date=date(2026, 9, 4),
            price_actual=Money(245_050, "RUB"),
        )

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        self.searched.append(number)
        if not self.has_order:
            return None
        return ShipmentResult(
            external_id=f"EXT-{number}",
            tracking_number=f"TRK-{number}",
            promised_delivery_date=None,
            price_actual=None,
        )

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        self.cancelled.append(ext_id)
        return CancelResult(
            accepted=self.cancel_accepted,
            message=None if self.cancel_accepted else "Заказ уже передан в доставку",
        )


@pytest.fixture
def carrier() -> ShippingCarrier:
    adapter = ShippingCarrier()
    registry.register(adapter)
    return adapter


async def _decision(client: AsyncClient, headers: dict[str, str], key: str = "d-1") -> str:
    """Пройти путь расчёт → рекомендация → решение и вернуть его идентификатор."""
    quote = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)
    assert quote.status_code == 200, quote.text
    body = quote.json()

    recommendation = await client.post(
        "/v1/routing/quote",
        json={"quote_id": body["quote_id"], "strategy": "optimal"},
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
        headers={**headers, "Idempotency-Key": key},
    )
    assert decision.status_code == 201, decision.text
    return str(decision.json()["decision_id"])


async def _create(
    client: AsyncClient, headers: dict[str, str], decision_id: str, key: str, **extra: Any
) -> Any:
    return await client.post(
        "/v1/shipments",
        json={"decision_id": decision_id, **extra},
        headers={**headers, "Idempotency-Key": key},
    )


class TestCreation:
    async def test_creates_an_order_at_the_carrier(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        decision_id = await _decision(client, headers)
        response = await _create(client, headers, decision_id, "s-1")

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "Created"
        assert body["external_id"] == f"EXT-{body['number']}"
        assert body["tracking_number"] == f"TRK-{body['number']}"
        assert body["decision_id"] == decision_id
        assert carrier.created == [body["number"]]

    async def test_a_created_shipment_enters_the_polling_queue(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Иначе трекинг не начинается никогда.

        Расписание опроса пересчитывается при каждом событии, но первому
        событию неоткуда взяться: пока срок опроса не назначен, фоновая
        задача это отправление не видит. Найдено на стенде — тестами
        не ловилось, потому что создание и приём событий проверялись порознь.
        """
        tenant_a, _ = carrier_setup
        decision_id = await _decision(client, headers)
        created = (await _create(client, headers, decision_id, "s-poll")).json()

        async with session_scope(tenant_a) as session:
            stored = await ShipmentRepository(session).get(UUID(created["id"]))

        assert stored is not None
        assert stored.next_poll_at is not None, "срок опроса не назначен — трекинг не начнётся"
        # Груз ещё не забран: по таблице FR-3.2 это час, а не немедленно.
        assert stored.next_poll_at - utcnow() < timedelta(hours=1, minutes=1)
        assert stored.next_poll_at > utcnow()

    async def test_the_quoted_price_is_kept_beside_the_actual_one(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Расхождение обещанного и фактического — предмет сверки счетов.

        Затереть обещание фактом значит потерять саму возможность сверки.
        """
        decision_id = await _decision(client, headers)
        body = (await _create(client, headers, decision_id, "s-2")).json()

        assert body["quoted_total_cost"]["amount_minor"] > 0
        assert body["actual_total_cost"]["amount_minor"] == 245_050

    async def test_a_fresh_draft_is_not_reconciled(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Черновик только что записан — значит к ТК по нему не обращались.

        Лишняя сверка на этом пути удвоила бы задержку создания, ничего
        не проверяя.
        """
        decision_id = await _decision(client, headers)
        await _create(client, headers, decision_id, "s-3")

        assert carrier.searched == []

    async def test_the_client_number_becomes_the_shipment_number(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Интеграция с ERP хочет видеть свой номер и у нас, и у перевозчика."""
        decision_id = await _decision(client, headers)
        body = (await _create(client, headers, decision_id, "s-4", external_id="ERP-77")).json()

        assert body["number"] == "ERP-77"
        assert carrier.created == ["ERP-77"]

    async def test_the_number_is_derived_from_the_idempotency_key(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Один и тот же ключ обязан давать один и тот же номер.

        По номеру идёт разговор с перевозчиком; разбирать инцидент проще,
        когда он воспроизводим, а не выдан случайно.
        """
        tenant_a, _ = carrier_setup
        decision_id = await _decision(client, headers)
        body = (await _create(client, headers, decision_id, "s-5")).json()

        assert body["number"] == shipment_number(tenant_a, "s-5")

    async def test_one_decision_gives_one_shipment(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Второе отправление по тому же выбору — два заказа на один груз."""
        decision_id = await _decision(client, headers)
        await _create(client, headers, decision_id, "s-6")
        second = await _create(client, headers, decision_id, "s-6-again")

        assert second.status_code == 409, second.text
        assert second.json()["error"]["field"] == "decision_id"
        assert len(carrier.created) == 1


class TestIdempotency:
    async def test_the_same_key_returns_the_same_shipment(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        decision_id = await _decision(client, headers)
        first = (await _create(client, headers, decision_id, "s-7")).json()
        second = await _create(client, headers, decision_id, "s-7")

        assert second.status_code == 201, second.text
        assert second.json()["id"] == first["id"]
        assert len(carrier.created) == 1, "перевозчика попросили создать заказ дважды"

    async def test_the_same_key_with_another_body_is_refused(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Клиент, изменивший тело, ждёт нового действия.

        Молча отдать ему прошлый результат хуже, чем честно отказать.
        """
        decision_id = await _decision(client, headers)
        await _create(client, headers, decision_id, "s-8")
        second = await _create(client, headers, decision_id, "s-8", external_id="ERP-99")

        assert second.status_code == 409, second.text
        assert second.json()["error"]["field"] == "Idempotency-Key"


class TestGhosts:
    """FR-2.5: заказ создан у перевозчика, ответ не дошёл."""

    async def test_a_lost_response_leaves_a_findable_draft(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Черновик обязан пережить откат транзакции запроса.

        Он записывается своей транзакцией именно ради этого: без него
        заказ у перевозчика остался бы без единой строки у нас.
        """
        carrier.create_behaviour = "timeout"
        decision_id = await _decision(client, headers)
        failed = await _create(client, headers, decision_id, "s-9")

        assert failed.status_code == 502, failed.text

        listing = await client.get("/v1/shipments", headers=headers)
        drafts = [s for s in listing.json()["items"] if s["status"] == "Draft"]
        assert len(drafts) == 1
        assert drafts[0]["number"] == shipment_number(carrier_setup[0], "s-9")
        assert drafts[0]["external_id"] is None

    async def test_the_retry_adopts_the_order_instead_of_creating_a_second(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Это и есть защита от «призрака»: сверка раньше повторного создания."""
        carrier.create_behaviour = "timeout"
        decision_id = await _decision(client, headers)
        assert (await _create(client, headers, decision_id, "s-10")).status_code == 502

        carrier.create_behaviour = "ok"
        retry = await _create(client, headers, decision_id, "s-10")

        assert retry.status_code == 201, retry.text
        body = retry.json()
        assert body["status"] == "Created"
        assert body["external_id"] == f"EXT-{body['number']}"
        assert carrier.searched == [body["number"]], "сверку не выполнили"
        assert len(carrier.created) == 1, "создали второй заказ поверх существующего"

    async def test_the_retry_creates_when_the_carrier_has_nothing(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Сверка не должна превращаться в отказ создавать.

        Если запрос до перевозчика не дошёл, заказа нет, и повтор обязан
        его создать — иначе клиент навсегда останется с черновиком.
        """
        carrier.create_behaviour = "timeout"
        decision_id = await _decision(client, headers)
        assert (await _create(client, headers, decision_id, "s-11")).status_code == 502

        carrier.create_behaviour = "ok"
        carrier.has_order = False  # запрос не дошёл: у ТК ничего нет
        retry = await _create(client, headers, decision_id, "s-11")

        assert retry.status_code == 201, retry.text
        assert len(carrier.created) == 2, "повтор обязан был создать заказ"


class TestCancellation:
    async def test_cancels_at_the_carrier_and_keeps_the_row(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Отправления не удаляются никогда: отмена — это состояние."""
        decision_id = await _decision(client, headers)
        created = (await _create(client, headers, decision_id, "s-12")).json()

        response = await client.post(f"/v1/shipments/{created['id']}/cancel", headers=headers)

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "Cancelled"
        assert carrier.cancelled == [created["external_id"]]
        assert (
            await client.get(f"/v1/shipments/{created['id']}", headers=headers)
        ).status_code == 200

    async def test_cancelling_twice_is_not_an_error(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Результат уже достигнут — отказывать не за что."""
        decision_id = await _decision(client, headers)
        created = (await _create(client, headers, decision_id, "s-13")).json()
        await client.post(f"/v1/shipments/{created['id']}/cancel", headers=headers)
        again = await client.post(f"/v1/shipments/{created['id']}/cancel", headers=headers)

        assert again.status_code == 200, again.text
        assert len(carrier.cancelled) == 1

    async def test_cancelling_a_draft_still_cancels_the_ghost(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Отменить черновик, бросив заказ у перевозчика, — это и есть призрак."""
        carrier.create_behaviour = "timeout"
        decision_id = await _decision(client, headers)
        assert (await _create(client, headers, decision_id, "s-14")).status_code == 502

        listing = await client.get("/v1/shipments", headers=headers)
        draft = next(s for s in listing.json()["items"] if s["status"] == "Draft")

        response = await client.post(f"/v1/shipments/{draft['id']}/cancel", headers=headers)

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "Cancelled"
        assert carrier.cancelled == [f"EXT-{draft['number']}"]

    async def test_a_carrier_refusal_is_reported_not_swallowed(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Отметить отменённым то, что перевозчик не отменил, — ложь в базе."""
        decision_id = await _decision(client, headers)
        created = (await _create(client, headers, decision_id, "s-15")).json()
        carrier.cancel_accepted = False

        response = await client.post(f"/v1/shipments/{created['id']}/cancel", headers=headers)

        assert response.status_code == 409, response.text
        assert "доставку" in response.json()["error"]["message"]
        after = await client.get(f"/v1/shipments/{created['id']}", headers=headers)
        assert after.json()["status"] == "Created"


class TestTenantIsolation:
    async def test_another_tenant_gets_404_not_403(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        carrier_setup: tuple[UUID, UUID],
        carrier: ShippingCarrier,
    ) -> None:
        """Наличие чужого объекта не подтверждается (раздел 7.2 ТЗ)."""
        from tests.conftest import login

        decision_id = await _decision(client, headers)
        created = (await _create(client, headers, decision_id, "s-16")).json()

        other = await login(client, "b@example.com")
        response = await client.get(f"/v1/shipments/{created['id']}", headers=other)

        assert response.status_code == 404
