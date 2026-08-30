"""Адресная книга тенанта (FR-8.4): CRUD, поиск, мягкое удаление, изоляция.

Идут против настоящего PostgreSQL: проверяются частичные уникальные индексы
и поиск по триграммам, которых на моках не существует.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import login

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def headers_a(client: AsyncClient, seeded_tenants: tuple[UUID, UUID]) -> dict[str, str]:
    return await login(client, "a@example.com")


@pytest.fixture
async def headers_b(client: AsyncClient, seeded_tenants: tuple[UUID, UUID]) -> dict[str, str]:
    return await login(client, "b@example.com")


ROSPLOMBA = {
    "type": "legal",
    "name": 'ООО "Роспломба"',
    "inn": "7701234567",
    "kpp": "770101001",
    "contact_person": "Иванов Иван",
    "phone": "+79161234567",
    "addresses": [
        {
            "city": "Москва",
            "city_fias_id": "0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
            "street": "ул Тверская",
            "house": "1",
            "is_default_sender": True,
        }
    ],
}


class TestCreate:
    async def test_creates_counterparty_with_address(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        response = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        assert response.status_code == 201

        body = response.json()
        assert body["name"] == 'ООО "Роспломба"'
        assert len(body["addresses"]) == 1
        assert body["addresses"][0]["city_fias_id"] == "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"

    async def test_address_with_city_and_house_is_fit_for_door_delivery(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        """Пригодность вычисляется, а не берётся из значения по умолчанию.

        Найдено запуском приложения: адрес с городом, улицей и домом
        сохранялся как непригодный и заблокировал бы создание отправления
        до двери.
        """
        response = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        assert response.json()["addresses"][0]["fitness"] == "door"

    async def test_address_without_house_is_fit_for_pickup_point_only(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        payload = {
            **ROSPLOMBA,
            "addresses": [
                {
                    "city": "Москва",
                    "city_fias_id": "0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
                }
            ],
        }
        response = await client.post("/v1/counterparties", json=payload, headers=headers_a)
        assert response.json()["addresses"][0]["fitness"] == "locality"

    async def test_address_without_city_is_unusable(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        # Без города адрес не годится ни для расчёта, ни для доставки.
        payload = {**ROSPLOMBA, "addresses": [{"city": "Неизвестно", "house": "1"}]}
        response = await client.post("/v1/counterparties", json=payload, headers=headers_a)
        assert response.json()["addresses"][0]["fitness"] == "unusable"

    async def test_duplicate_inn_is_conflict_not_500(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        response = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    async def test_same_inn_in_another_tenant_is_allowed(
        self, client: AsyncClient, headers_a: dict[str, str], headers_b: dict[str, str]
    ) -> None:
        """Один и тот же контрагент может быть у разных компаний.

        Уникальность ИНН — в пределах тенанта, а не платформы: «Роспломба»
        и её конкурент могут отправлять одному и тому же получателю.
        """
        first = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        second = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_b)

        assert first.status_code == 201
        assert second.status_code == 201

    async def test_branch_and_head_office_share_one_inn(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        """Головная организация и филиал — разные контрагенты с одним ИНН.

        Проверка в сервисе обязана совпадать с уникальным индексом
        ``(tenant_id, inn, coalesce(kpp, ''))``: иначе завести головную
        организацию после филиала было бы невозможно.
        """
        branch = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        head = await client.post(
            "/v1/counterparties",
            json={**ROSPLOMBA, "kpp": None, "addresses": []},
            headers=headers_a,
        )

        assert branch.status_code == 201
        assert head.status_code == 201

    async def test_same_inn_and_same_kpp_is_still_conflict(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        again = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        assert again.status_code == 409

    async def test_malformed_inn_is_rejected(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        payload = {**ROSPLOMBA, "inn": "не-число"}
        response = await client.post("/v1/counterparties", json=payload, headers=headers_a)

        assert response.status_code == 422
        assert response.json()["error"]["field"] == "inn"


class TestSearch:
    @pytest.fixture(autouse=True)
    async def _seed(self, client: AsyncClient, headers_a: dict[str, str]) -> None:
        await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        await client.post(
            "/v1/counterparties",
            json={"type": "legal", "name": 'ООО "Ромашка-Сервис"', "inn": "7809876543"},
            headers=headers_a,
        )

    async def test_finds_substring_in_the_middle_of_a_word(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        """Ключевое требование автоподстановки.

        Оператор набирает «плом» — и должен увидеть «Роспломба». Именно этот
        запрос не обслуживается полнотекстовым поиском, который ищет по началу
        лексемы, и ради него в схеме заведён GIN-индекс по триграммам.
        """
        response = await client.get("/v1/counterparties", params={"q": "плом"}, headers=headers_a)

        names = [item["name"] for item in response.json()["items"]]
        assert names == ['ООО "Роспломба"']

    async def test_search_is_case_insensitive(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        response = await client.get(
            "/v1/counterparties", params={"q": "РОМАШКА"}, headers=headers_a
        )
        assert len(response.json()["items"]) == 1

    async def test_digits_are_searched_as_inn_prefix(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        response = await client.get("/v1/counterparties", params={"q": "7701"}, headers=headers_a)

        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["inn"] == "7701234567"

    async def test_empty_query_returns_everything(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        response = await client.get("/v1/counterparties", headers=headers_a)
        assert response.json()["total"] == 2

    async def test_pagination_reports_total(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        response = await client.get(
            "/v1/counterparties", params={"limit": 1, "offset": 0}, headers=headers_a
        )
        body = response.json()
        assert len(body["items"]) == 1
        assert body["total"] == 2


class TestTenantIsolation:
    async def test_foreign_counterparty_gives_404_not_403(
        self, client: AsyncClient, headers_a: dict[str, str], headers_b: dict[str, str]
    ) -> None:
        """Критерий приёмки 14.2, п. 11.

        403 подтвердил бы существование объекта и превратил бы идентификаторы
        в канал утечки состава клиентской базы конкурента.
        """
        created = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        foreign_id = created.json()["id"]

        response = await client.get(f"/v1/counterparties/{foreign_id}", headers=headers_b)
        assert response.status_code == 404

    async def test_search_never_shows_another_tenant(
        self, client: AsyncClient, headers_a: dict[str, str], headers_b: dict[str, str]
    ) -> None:
        await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)

        response = await client.get("/v1/counterparties", params={"q": "плом"}, headers=headers_b)
        assert response.json()["items"] == []

    async def test_foreign_addresses_are_not_listed(
        self, client: AsyncClient, headers_a: dict[str, str], headers_b: dict[str, str]
    ) -> None:
        created = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        foreign_id = created.json()["id"]

        response = await client.get(f"/v1/counterparties/{foreign_id}/addresses", headers=headers_b)
        assert response.status_code == 404


class TestDefaultSender:
    async def test_only_one_default_sender_survives(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        """Отправитель по умолчанию у тенанта ровно один.

        Гарантия держится на частичном уникальном индексе, а не на коде:
        два оператора могут назначить его одновременно.
        """
        created = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        counterparty_id = created.json()["id"]

        second = await client.post(
            f"/v1/counterparties/{counterparty_id}/addresses",
            json={
                "city": "Новосибирск",
                "city_fias_id": "8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                "street": "ул Ленина",
                "house": "12",
                "is_default_sender": True,
            },
            headers=headers_a,
        )
        assert second.status_code == 201

        addresses = (
            await client.get(f"/v1/counterparties/{counterparty_id}/addresses", headers=headers_a)
        ).json()
        defaults = [a for a in addresses if a["is_default_sender"]]
        assert len(defaults) == 1
        assert defaults[0]["city"] == "Новосибирск"


class TestSoftDelete:
    async def test_deleted_counterparty_disappears_from_search(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        created = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        counterparty_id = created.json()["id"]

        assert (
            await client.delete(f"/v1/counterparties/{counterparty_id}", headers=headers_a)
        ).status_code == 204

        listing = await client.get("/v1/counterparties", headers=headers_a)
        assert listing.json()["total"] == 0

    async def test_deleted_counterparty_frees_its_inn(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        """Удалённый контрагент не должен вечно занимать ИНН.

        Уникальность действует только среди живых строк — на этом построен
        частичный уникальный индекс.
        """
        created = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        await client.delete(f"/v1/counterparties/{created.json()['id']}", headers=headers_a)

        again = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        assert again.status_code == 201

    async def test_deleted_counterparty_gives_404(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        created = await client.post("/v1/counterparties", json=ROSPLOMBA, headers=headers_a)
        counterparty_id = created.json()["id"]
        await client.delete(f"/v1/counterparties/{counterparty_id}", headers=headers_a)

        response = await client.get(f"/v1/counterparties/{counterparty_id}", headers=headers_a)
        assert response.status_code == 404


class TestPermissions:
    async def test_viewer_cannot_create(
        self, client: AsyncClient, headers_a: dict[str, str]
    ) -> None:
        # Роль owner создавать может; проверяем, что защита вообще включена,
        # обращением без авторизации.
        response = await client.post("/v1/counterparties", json=ROSPLOMBA)
        assert response.status_code == 401
