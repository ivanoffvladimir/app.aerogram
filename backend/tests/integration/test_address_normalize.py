"""Нормализация адреса целиком: от ответа ДаData до строки в справочнике.

Юнит-тесты проверяют разбор ответа, а этот — то, что сервис делает с
разобранным: возвращает координаты вызывающему и НЕ кладёт их в общую
таблицу городов.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.directories.dadata import DadataClient
from aerogram.directories.repository import CityRepository
from aerogram.directories.schemas import DadataAddressData
from aerogram.directories.service import AddressService

pytestmark = pytest.mark.integration

MOSCOW = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"

#: Ответ стандартизации на «г Москва, ул Тверская, д 1»: ключ города — Москва
#: (уровень 1), а ``fias_id`` и координаты принадлежат ДОМУ.
TVERSKAYA = DadataAddressData(
    country_iso_code="RU",
    region="Москва",
    region_with_type="г Москва",
    region_fias_id=MOSCOW,
    region_kladr_id="7700000000000",
    city="Москва",
    city_with_type="г Москва",
    city_fias_id=MOSCOW,
    city_kladr_id="7700000000000",
    street="Тверская",
    street_with_type="ул Тверская",
    house="1",
    postal_code="125009",
    fias_id="1e5fb5c6-0b4b-4b2f-8e5b-4b9e3a2f1c00",
    fias_level="8",
    geo_lat="55.7576200",
    geo_lon="37.6144100",
    timezone="UTC+3",
    qc_geo="0",
)


class _FakeDadata(DadataClient):
    """Стандартизация без сети: проверяется сервис, а не HTTP-клиент."""

    def __init__(self, client: httpx.AsyncClient, data: DadataAddressData | None) -> None:
        super().__init__(token="test-token", secret="test-secret", client=client)
        self._data = data

    async def clean_address(self, query: str) -> DadataAddressData | None:
        return self._data


@pytest.fixture
async def dadata() -> AsyncIterator[httpx.AsyncClient]:
    client = httpx.AsyncClient()
    yield client
    await client.aclose()


async def test_coordinates_reach_the_caller_but_not_the_city_directory(
    session: AsyncSession, dadata: httpx.AsyncClient
) -> None:
    """Координаты дома — персональные данные получателя.

    ``addresses`` под RLS и принадлежит тенанту, ``cities`` общая для всех
    и без RLS (12.1, 12.7 ТЗ). Поэтому координаты уходят вызывающему,
    который положит их в адрес, но в справочник городов не попадают.
    """
    service = AddressService(session, _FakeDadata(dadata, TVERSKAYA))

    result = await service.normalize("г Москва, ул Тверская, д 1")

    assert result.city_fias_id == MOSCOW
    assert (result.lat, result.lon) == (55.75762, 37.61441)
    assert result.geo_precision == "house"
    assert result.fitness == "door"

    city = await CityRepository(session).get_by_fias(MOSCOW)
    assert city is not None
    assert city.lat is None
    assert city.lon is None
    assert city.full_name is not None
    assert "Тверская" not in city.full_name


async def test_unavailable_dadata_gives_address_without_coordinates(
    session: AsyncSession, dadata: httpx.AsyncClient
) -> None:
    """Путь деградации: адрес не нормализован, координат нет, ошибки тоже нет."""
    service = AddressService(session, _FakeDadata(dadata, None))

    result = await service.normalize("совершенно неизвестный адрес")

    assert result.fitness == "unusable"
    assert result.lat is None
    assert result.lon is None
    assert result.geo_precision is None
