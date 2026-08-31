"""Список перевозчиков и состояние подключения (`GET /v1/carriers`).

Путь объявлен в `docs/tz/v3/openapi.yaml` и до сих пор не был реализован:
кабинет не мог показать, кто подключён, по чьему договору считается цена
и что нужно ввести, чтобы подключить остальных.

Главное, что здесь проверяется, — что в ответе НЕТ учётных данных. Состав
полей не секрет, их содержимое — секрет, и обратно клиент его не получает
(CLAUDE.md §6).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient

from tests.conftest import login
from tests.integration.conftest import TEST_KEY

pytestmark = pytest.mark.integration


def by_code(rows: list[dict], code: str) -> dict:
    return next(row for row in rows if row["code"] == code)


class TestConnectedCarriers:
    async def test_a_connected_carrier_shows_its_contract_mode(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Чей тариф считается — договорной вопрос, и его видно сразу."""
        response = await client.get("/v1/carriers", headers=headers)

        assert response.status_code == 200, response.text
        row = by_code(response.json(), "fake")
        assert row["connected"] is True
        assert row["mode"] == "own_contract"

    async def test_credentials_never_come_back(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Ни в открытом виде, ни шифротекстом, ни под другим именем."""
        response = await client.get("/v1/carriers", headers=headers)
        body = response.text

        assert "test-secret" not in body
        assert "test-id" not in body
        assert "credentials" not in body
        # Шифротекст учётной записи из фикстуры тоже не должен просочиться.
        assert TEST_KEY.split(":", 1)[1] not in body

    async def test_a_carrier_without_an_account_is_still_listed(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Экран подключения существует ради неподключённых.

        Второй тенант не подключал никого — и должен увидеть, кого может.
        """
        other = await login(client, "b@example.com")
        rows = (await client.get("/v1/carriers", headers=other)).json()

        row = by_code(rows, "fake")
        assert row["connected"] is False
        assert row["mode"] is None
        assert row["status"] is None

    async def test_another_tenants_connection_is_not_visible(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Иначе конкурент увидел бы, с кем у соседа договор."""
        mine = by_code((await client.get("/v1/carriers", headers=headers)).json(), "fake")
        other = await login(client, "b@example.com")
        theirs = by_code((await client.get("/v1/carriers", headers=other)).json(), "fake")

        assert mine["connected"] is True
        assert theirs["connected"] is False

    async def test_authorisation_is_required(self, client: AsyncClient) -> None:
        response = await client.get("/v1/carriers")

        assert response.status_code == 401
