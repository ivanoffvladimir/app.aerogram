"""Чтение городов по идентификаторам ФИАС.

Подсказки отвечают на «какой город имеется в виду», этот путь — на «как
называется тот, что уже выбран». Без него условие правила маршрутизации,
хранящее города списком ФИАС, нечитаемо: экран может только сосчитать,
сколько их.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.integration.conftest import MOSCOW, VLADIVOSTOK

pytestmark = pytest.mark.asyncio

UNKNOWN = "00000000-0000-4000-8000-000000000000"


async def _cities(client: AsyncClient, headers: dict[str, str], *ids: str) -> list[dict[str, Any]]:
    query = "&".join(f"fias_id={value}" for value in ids)
    response = await client.get(f"/v1/cities?{query}", headers=headers)
    assert response.status_code == 200, response.text
    result: list[dict[str, Any]] = response.json()
    return result


class TestLookup:
    async def test_names_come_back_for_known_ids(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[Any, Any]
    ) -> None:
        items = await _cities(client, headers, MOSCOW, VLADIVOSTOK)
        assert {item["name"] for item in items} == {"Москва", "Владивосток"}
        assert {item["fias_id"] for item in items} == {MOSCOW, VLADIVOSTOK}

    async def test_an_unknown_id_shortens_the_answer_and_is_not_an_error(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[Any, Any]
    ) -> None:
        """Город мог исчезнуть из справочника, а правило с ним — остаться.

        Отказывать в показе всего правила из-за одного города незачем.
        """
        items = await _cities(client, headers, MOSCOW, UNKNOWN)
        assert [item["fias_id"] for item in items] == [MOSCOW]

    async def test_the_answer_is_matched_by_fias_and_not_by_position(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[Any, Any]
    ) -> None:
        """Порядок ответа не совпадает с порядком запроса — и не должен.

        Часть идентификаторов может не найтись, и сопоставление по позиции
        назвало бы один город именем другого.
        """
        items = await _cities(client, headers, VLADIVOSTOK, MOSCOW)
        by_fias = {item["fias_id"]: item["name"] for item in items}
        assert by_fias[MOSCOW] == "Москва"
        assert by_fias[VLADIVOSTOK] == "Владивосток"

    async def test_at_least_one_identifier_is_required(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        """Запрос без списка — это «отдай весь справочник», а не пустой ответ."""
        response = await client.get("/v1/cities", headers=headers)
        assert response.status_code == 422, response.text

    async def test_a_long_list_is_refused(
        self, client: AsyncClient, headers: dict[str, str]
    ) -> None:
        query = "&".join(f"fias_id={UNKNOWN}" for _ in range(51))
        response = await client.get(f"/v1/cities?{query}", headers=headers)
        assert response.status_code == 422, response.text

    async def test_authorisation_is_required(self, client: AsyncClient) -> None:
        response = await client.get(f"/v1/cities?fias_id={MOSCOW}")
        assert response.status_code == 401, response.text
