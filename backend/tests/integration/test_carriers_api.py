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

from aerogram.carriers import registry
from aerogram.core.repository import CarrierAccountRepository
from aerogram.db import session_scope
from tests.conftest import TEST_PASSWORD, login
from tests.integration.conftest import TEST_KEY, FakeCarrier

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

    async def test_the_volumetric_divisor_is_visible(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Величина договорная, и расхождение с договором должен заметить
        человек в кабинете, а не счёт от перевозчика через месяц."""
        rows = (await client.get("/v1/carriers", headers=headers)).json()

        assert by_code(rows, "fake")["volumetric_divisor"] == 5000

    async def test_authorisation_is_required(self, client: AsyncClient) -> None:
        response = await client.get("/v1/carriers")

        assert response.status_code == 401


class TestConnectionCheck:
    """«Проверить подключение» — системное ТЗ, раздел 9; фронт-ТЗ, раздел 8.

    До этого `status`, `status_message` и `last_check_at` существовали
    в схеме и не заполнялись никогда: список показывал «не проверялось»,
    чем бы дело ни кончилось.
    """

    @staticmethod
    def _register(behaviour: str = "ok") -> FakeCarrier:
        registry._reset_for_tests()
        adapter = FakeCarrier("fake", behaviour=behaviour)
        registry.register(adapter)
        return adapter

    async def test_a_working_account_is_reported_and_remembered(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        self._register()

        response = await client.post("/v1/carriers/fake/check", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_healthy"] is True
        assert body["status"] == "ok"
        assert body["message"] is None
        # Задержку требует бэкенд-ТЗ: health/latency.
        assert body["latency_ms"] >= 0
        assert body["checked_at"]

        # И это осело в учётной записи, а не осталось в ответе.
        listed = by_code((await client.get("/v1/carriers", headers=headers)).json(), "fake")
        assert listed["status"] == "ok"

    async def test_a_refusal_is_an_answer_with_200_not_an_error(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Вопрос был «работают ли доступы». «Не работают» — ответ на него."""
        self._register("error")

        response = await client.post("/v1/carriers/fake/check", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_healthy"] is False
        assert body["status"] == "error"
        assert body["message"] == "Перевозчик отклонил учётные данные"

        listed = by_code((await client.get("/v1/carriers", headers=headers)).json(), "fake")
        assert listed["status"] == "error"
        assert listed["status_message"] == "Перевозчик отклонил учётные данные"

    async def test_a_broken_adapter_does_not_give_a_500(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Ошибка перевозчика никогда не даёт 500 (CLAUDE.md §6).

        Здесь адаптер падает необработанным исключением — самый неудобный
        случай, и именно он раньше уходил бы клиенту пятисотой.
        """
        self._register("crash")

        response = await client.post("/v1/carriers/fake/check", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_healthy"] is False
        # Наша ошибка называется нашей: иначе клиент пойдёт перевыпускать
        # доступы, с которыми всё в порядке.
        assert "платформы" in body["message"]

    async def test_an_unregistered_adapter_says_so(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        registry._reset_for_tests()

        response = await client.post("/v1/carriers/fake/check", headers=headers)

        assert response.status_code == 200, response.text
        assert response.json()["is_healthy"] is False
        assert "не подключён к платформе" in response.json()["message"]

    async def test_unreadable_credentials_ask_to_enter_them_again(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Отозванный ключ шифрования — не отказ перевозчика.

        Разница важна: тут доступы нужно ввести заново, а не выпрашивать
        у перевозчика.
        """
        self._register()
        tenant_a, _ = carrier_setup
        async with session_scope(tenant_a) as session:
            account = (await CarrierAccountRepository(session).list_active())[0]
            account.credentials_encrypted = "k1:не-шифротекст"

        response = await client.post("/v1/carriers/fake/check", headers=headers)

        assert response.status_code == 200, response.text
        assert response.json()["is_healthy"] is False
        assert "введите их заново" in response.json()["message"]

    async def test_an_unknown_carrier_is_404(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        response = await client.post("/v1/carriers/несуществующий/check", headers=headers)
        assert response.status_code == 404

    async def test_a_carrier_without_an_account_is_refused_with_a_reason(
        self,
        client: AsyncClient,
        seeded_tenants: tuple[UUID, UUID],
        carrier_setup: tuple[UUID, UUID],
    ) -> None:
        """Второй тенант никого не подключал: проверять нечего, и это
        не «перевозчик сломан», а «сначала введите доступы»."""
        self._register()
        other = await login(client, "b@example.com")

        response = await client.post("/v1/carriers/fake/check", headers=other)

        # 422 — тот же код, каким отвечает вся остальная валидация.
        assert response.status_code == 422, response.text
        assert "не подключён" in response.json()["error"]["message"]

    async def test_credentials_never_come_back_from_the_check(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        self._register("error")

        body = (await client.post("/v1/carriers/fake/check", headers=headers)).text

        assert "test-secret" not in body
        assert "test-id" not in body
        assert TEST_KEY.split(":", 1)[1] not in body

    async def test_an_operator_cannot_spend_a_carrier_call(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[UUID, UUID]
    ) -> None:
        """Проверка тратит вызов у перевозчика и пишет в учётную запись."""
        self._register()
        created = await client.post(
            "/v1/users",
            json={
                "email": "operator-check@example.com",
                "full_name": "Оператор",
                "role": "operator",
                "password": TEST_PASSWORD,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text

        operator = await login(client, "operator-check@example.com")
        response = await client.post("/v1/carriers/fake/check", headers=operator)

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"
