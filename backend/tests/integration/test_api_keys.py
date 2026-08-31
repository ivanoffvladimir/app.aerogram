"""Ключи машинного доступа: выпуск, отзыв и область действия (FR-10.2).

До этого выпустить ключ было нельзя ни одним путём — сервис существовал,
эндпоинтов не было, — то есть весь машинный доступ, ради которого написана
аутентификация по `X-Api-Key`, снаружи был недостижим.

Область действия проверяется здесь по-настоящему, через HTTP: таблица
в ``core.scopes`` описывает намерение, а ценность имеет только то, что
действительно отказывает.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient

from tests.integration.conftest import RATE_REQUEST

pytestmark = pytest.mark.asyncio


async def _issue(
    client: AsyncClient, headers: dict[str, str], scopes: list[str], name: str = "1С"
) -> tuple[str, dict]:
    """Выпустить ключ через API. Возвращает секрет и карточку ключа."""
    response = await client.post(
        "/v1/api-keys", json={"name": name, "scopes": scopes}, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["secret"], body["key"]


class TestIssuing:
    async def test_the_secret_is_shown_once_and_the_hash_never(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """В базе лежит хеш; отдать значение повторно нельзя и не нужно."""
        secret, key = await _issue(client, headers, ["rates:read"])

        listing = await client.get("/v1/api-keys", headers=headers)

        assert secret.startswith("ak_")
        assert key["prefix"] == secret[:16]
        body = listing.text
        assert secret not in body
        assert "key_hash" not in body
        assert listing.json()[0]["scopes"] == ["rates:read"]

    async def test_an_unknown_scope_is_refused(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Иначе клиент считал бы, что доступ выдан, и получал бы 403."""
        response = await client.post(
            "/v1/api-keys",
            json={"name": "1С", "scopes": ["rates:read", "shipments:delete"]},
            headers=headers,
        )

        assert response.status_code == 422
        assert response.json()["error"]["field"] == "scopes"

    async def test_a_key_without_scopes_is_refused(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Ключ без областей ничего не может: выдать такой — отдать нерабочий."""
        response = await client.post(
            "/v1/api-keys", json={"name": "1С", "scopes": []}, headers=headers
        )

        assert response.status_code == 422


class TestScopeIsEnforced:
    async def test_a_key_within_its_scope_works(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        secret, _ = await _issue(client, headers, ["rates:read"])

        response = await client.post("/v1/rates", json=RATE_REQUEST, headers={"X-Api-Key": secret})

        assert response.status_code == 200, response.text

    async def test_a_key_outside_its_scope_is_refused(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Ключ «только расчёт» не должен создавать отправления.

        До проверки областей поле в базе было обещанием, которое ничто
        не поддерживало: такой ключ создавал и отменял заказы у перевозчика.
        """
        secret, _ = await _issue(client, headers, ["rates:read"])

        response = await client.post(
            "/v1/shipments",
            json={"decision_id": "01a05792-0000-7000-8000-000000000000"},
            headers={"X-Api-Key": secret, "Idempotency-Key": "k-1"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_stolen_key_cannot_issue_another_one(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Иначе отзыв ключа ничего не даёт: у похитителя уже есть второй."""
        secret, _ = await _issue(client, headers, ["rates:read", "shipments:write"])

        response = await client.post(
            "/v1/api-keys",
            json={"name": "мой второй", "scopes": ["shipments:write"]},
            headers={"X-Api-Key": secret},
        )

        assert response.status_code == 403

    async def test_a_key_cannot_manage_users(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Заведённый пользователь пережил бы отзыв ключа."""
        secret, _ = await _issue(client, headers, ["rates:read"])

        response = await client.get("/v1/users", headers={"X-Api-Key": secret})

        assert response.status_code == 403

    async def test_the_caller_can_still_ask_who_it_is(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Данных тенанта этот путь не отдаёт, а клиенту нужен для проверки связи."""
        secret, _ = await _issue(client, headers, ["rates:read"])

        response = await client.get("/v1/auth/me", headers={"X-Api-Key": secret})

        assert response.status_code == 200, response.text
        assert response.json()["role"] == "api_client"
        # Ветка машинного клиента здесь падала с 500: в подставном адресе
        # стоял домен `.local`, который EmailStr не принимает.
        assert response.json()["email"].endswith("@example.com")


class TestRevoking:
    async def test_a_revoked_key_stops_working_at_once(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        secret, key = await _issue(client, headers, ["rates:read"])

        revoked = await client.delete(f"/v1/api-keys/{key['id']}", headers=headers)
        after = await client.post("/v1/rates", json=RATE_REQUEST, headers={"X-Api-Key": secret})

        assert revoked.status_code == 204
        assert after.status_code == 401
        assert (await client.get("/v1/api-keys", headers=headers)).json() == []

    async def test_a_foreign_key_is_not_found(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Чужой ключ — 404, а не 403: подтверждать его существование нельзя."""
        from tests.conftest import login

        _, key = await _issue(client, headers, ["rates:read"])
        other = await login(client, "b@example.com")

        response = await client.delete(f"/v1/api-keys/{key['id']}", headers=other)

        assert response.status_code == 404
