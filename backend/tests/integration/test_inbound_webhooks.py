"""Приём вебхуков от перевозчиков (FR-3.1, решение — ADR-0015).

Здесь проверяется не «доходит ли событие», а границы, на которых ошибка
дороже всего. Путь публичный: он объявлен в контракте с ``security: []``,
и вместо токена у него подпись. Отправление ищется узким окном поверх RLS,
и это окно — то единственное место, где данные видны без тенанта.

Поэтому отдельно проверяется само окно: что через него нельзя писать
и что оно не открывает ничего, кроме отправлений.
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers import registry
from aerogram.carriers.base import RawEvent, WebhookUpdate
from aerogram.shared.crypto import CredentialCipher
from aerogram.tracking.inbound import CREDENTIAL_FIELD
from tests.integration.conftest import TEST_KEY, TrackingCarrier
from tests.integration.test_tracking_api import _shipment

pytestmark = pytest.mark.asyncio

SECRET = "webhook-secret-of-this-tenant"
OCCURRED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class WebhookCarrier(TrackingCarrier):
    """Перевозчик, умеющий подписывать и разбирать вебхук."""

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        return [
            WebhookUpdate(
                external_id=str(payload["order"]),
                events=(
                    RawEvent(
                        occurred_at=OCCURRED,
                        status_raw=str(payload["status"]),
                        city="Москва",
                    ),
                ),
            )
        ]

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        expected = hmac.new(secret.encode(), payload, "sha256").hexdigest()
        return hmac.compare_digest(expected, headers.get("x-signature", ""))


@pytest.fixture
def webhook_carrier(cdek_setup: UUID) -> WebhookCarrier:
    registry._reset_for_tests()
    adapter = WebhookCarrier()
    registry.register(adapter)
    return adapter


@pytest.fixture
async def with_secret(cdek_setup: UUID, database_url: str) -> UUID:
    """Дописать секрет подписи в учётные данные тенанта.

    Секрет лежит в том же зашифрованном конверте, что и остальные доступы:
    это секрет, и в ``settings`` (JSONB без шифрования) ему не место.
    """
    tenant_a = cdek_setup
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cipher = CredentialCipher({"k1": TEST_KEY.split(":", 1)[1]}, "k1")

    async with factory() as db, db.begin():
        await db.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)})
        row = (await db.execute(text("SELECT id FROM carrier_accounts LIMIT 1"))).scalar_one()
        payload = json.dumps({"client_id": "i", "client_secret": "s", CREDENTIAL_FIELD: SECRET})
        await db.execute(
            text("UPDATE carrier_accounts SET credentials_encrypted = :c WHERE id = :id"),
            {"c": cipher.encrypt(payload, aad=str(row).encode()), "id": row},
        )
    await engine.dispose()
    return tenant_a


def signed(body: dict[str, Any], secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    return raw, {"x-signature": hmac.new(secret.encode(), raw, "sha256").hexdigest()}


async def timeline(client: AsyncClient, headers: dict[str, str], shipment_id: str) -> list[dict]:
    response = await client.get(f"/v1/shipments/{shipment_id}/tracking", headers=headers)
    assert response.status_code == 200, response.text
    return list(response.json())


class TestInboundWebhook:
    async def test_a_signed_event_reaches_the_timeline(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        webhook_carrier: WebhookCarrier,
        with_secret: UUID,
    ) -> None:
        """Ради этого функция и существует: статус без ожидания опроса."""
        shipment = await _shipment(client, headers, key="wh-1")
        body, sig = signed({"order": f"EXT-{shipment['number']}", "status": "DELIVERED"})

        # Без заголовка авторизации намеренно: перевозчик наш токен не носит.
        response = await client.post("/v1/webhooks/cdek", content=body, headers=sig)

        assert response.status_code == 202, response.text
        assert response.json()["accepted"] == 1
        events = await timeline(client, headers, shipment["id"])
        assert [e["normalized_status"] for e in events] == ["DELIVERED"]
        assert events[0]["carrier_status"] == "DELIVERED"

    async def test_a_wrong_signature_changes_nothing(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        webhook_carrier: WebhookCarrier,
        with_secret: UUID,
    ) -> None:
        """Лента — основание для Carrier Score и для разбора спора.

        Непроверенное событие в ней означает, что оспорить его нечем.
        """
        shipment = await _shipment(client, headers, key="wh-2")
        body, _ = signed({"order": f"EXT-{shipment['number']}", "status": "DELIVERED"})

        response = await client.post(
            "/v1/webhooks/cdek", content=body, headers={"x-signature": "0" * 64}
        )

        assert response.status_code == 401, response.text
        assert await timeline(client, headers, shipment["id"]) == []

    async def test_an_unknown_order_is_accepted_and_ignored(
        self, client: AsyncClient, webhook_carrier: WebhookCarrier, with_secret: UUID
    ) -> None:
        """Не 404: иначе перевозчик повторял бы доставку до посинения
        из-за заказа, созданного вообще не через нас."""
        body, sig = signed({"order": "EXT-НЕ-НАШ", "status": "DELIVERED"})

        response = await client.post("/v1/webhooks/cdek", content=body, headers=sig)

        assert response.status_code == 202, response.text
        assert response.json()["accepted"] == 0

    async def test_an_unknown_carrier_is_not_found(
        self, client: AsyncClient, webhook_carrier: WebhookCarrier
    ) -> None:
        body, sig = signed({"order": "EXT-1", "status": "DELIVERED"})

        response = await client.post("/v1/webhooks/unknown-carrier", content=body, headers=sig)

        assert response.status_code == 404

    async def test_the_same_event_twice_creates_one_row(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        webhook_carrier: WebhookCarrier,
        with_secret: UUID,
    ) -> None:
        """Перевозчик повторяет доставку, пока не получит 2xx."""
        shipment = await _shipment(client, headers, key="wh-3")
        body, sig = signed({"order": f"EXT-{shipment['number']}", "status": "DELIVERED"})

        first = await client.post("/v1/webhooks/cdek", content=body, headers=sig)
        second = await client.post("/v1/webhooks/cdek", content=body, headers=sig)

        assert first.json()["accepted"] == 1
        assert second.json()["accepted"] == 0
        assert len(await timeline(client, headers, shipment["id"])) == 1

    async def test_a_body_that_is_not_json_is_refused(
        self, client: AsyncClient, webhook_carrier: WebhookCarrier
    ) -> None:
        response = await client.post(
            "/v1/webhooks/cdek", content=b"<xml/>", headers={"x-signature": "x"}
        )

        assert response.status_code == 422


class TestTheWindowIsNarrow:
    """Окно ``app.auth_scope = 'webhook'`` — единственное место, где строки
    видны без тенанта. Его границы важнее удобства.

    Во всех трёх проверках отправление создаётся заранее: на пустой таблице
    любой запрос вернёт ноль строк, и тест зеленел бы при какой угодно
    политике, ничего не проверяя.
    """

    async def test_it_does_not_allow_writing(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        webhook_carrier: WebhookCarrier,
        database_url: str,
    ) -> None:
        """Окно объявлено только на SELECT.

        Приём событий идёт уже под тенантом найденного отправления; если бы
        через окно можно было писать, вебхук менял бы чужие строки.
        """
        await _shipment(client, headers, key="wnd-1")

        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.auth_scope', 'webhook', true)"))
            seen = (await conn.execute(text("SELECT count(*) FROM shipments"))).scalar_one()
            updated = await conn.execute(
                text("UPDATE shipments SET comment = 'через окно' RETURNING id")
            )
            written = list(updated.scalars())
        await engine.dispose()

        assert seen == 1, "окно должно показывать отправление — иначе проверять нечего"
        assert written == [], "через окно поиска нельзя писать"

    async def test_it_opens_nothing_but_shipments(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        webhook_carrier: WebhookCarrier,
        database_url: str,
    ) -> None:
        """Значение окна отдельное от ``login`` и ``api_key`` именно затем,
        чтобы окно на отправления не открывало пользователей и ключи."""
        await _shipment(client, headers, key="wnd-2")

        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.auth_scope', 'webhook', true)"))
            shipments = (await conn.execute(text("SELECT count(*) FROM shipments"))).scalar_one()
            users = (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one()
        await engine.dispose()

        # Пользователи в базе есть — их заводит фикстура тенантов.
        assert shipments == 1
        assert users == 0, "окно вебхука не должно открывать пользователей"

    async def test_without_the_window_shipments_stay_invisible(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        webhook_carrier: WebhookCarrier,
        database_url: str,
    ) -> None:
        """Иначе окно ничего не открывало бы, а тест ничего не проверял."""
        await _shipment(client, headers, key="wnd-3")

        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            visible = (await conn.execute(text("SELECT count(*) FROM shipments"))).scalar_one()
        await engine.dispose()

        assert visible == 0
