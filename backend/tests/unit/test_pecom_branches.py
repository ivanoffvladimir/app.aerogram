"""Справочник подразделений ПЭК (FR-8.3).

Фикстура синтетическая — см. tests/fixtures/pecom/README.md. Собрана по разделу
«Операции с филиалами» официальной справки, дословно повторяя структуру примера
ответа из неё, включая склад с нулевыми ограничениями и отделение с пустым
массивом складов.

Дорого здесь ровно одно: в ответе ЧЕТЫРЕ идентификатора, и в расчёте стоимости
годится только идентификатор склада. Справка предупреждает об этом капслоком,
и цена ошибки соответствующая: любой другой ПЭК отвергнет, а узнается это
не при синхронизации, а на первом расчёте у клиента.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount
from aerogram.carriers.pecom.adapter import PecomAdapter
from aerogram.carriers.pecom.branches import BRANCHES_PATH, parse_branches
from aerogram.carriers.pecom.client import SANDBOX_BASE_URL, PecomClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pecom"

#: Идентификаторы из фикстуры, названные по смыслу: половина теста
#: про то, чтобы не перепутать их между собой.
MAIN_WAREHOUSE = "c496b0c6-8e45-11df-bb3b-0019bbc941ce"
PVZ_WAREHOUSE = "f7a10d22-8e45-11df-bb3b-0019bbc941ce"
DIVISION_ID = "8112fb89-5a96-11e4-94e4-00155d9d920f"
CITY_ID = "22f8a64a-9647-428f-89a6-1208c4edf756"
BRANCH_ID = "e5c3ede6-5ce7-11df-ac33-0017085a0478"


@pytest.fixture
def catalog() -> Any:
    body = json.loads((FIXTURES / "branches_all.json").read_text(encoding="utf-8"))
    return parse_branches(body)


class TestTerminals:
    def test_one_row_per_warehouse(self, catalog: Any) -> None:
        # Отделение с пустым массивом складов (Москва) — закрывающееся,
        # терминалом оно быть не должно.
        assert len(catalog.terminals) == 2

    def test_code_is_the_warehouse_id(self, catalog: Any) -> None:
        codes = {row.external_code for row in catalog.terminals}
        assert codes == {MAIN_WAREHOUSE, PVZ_WAREHOUSE}
        # Ни один другой идентификатор из ответа сюда попасть не должен.
        assert DIVISION_ID not in codes
        assert CITY_ID not in codes
        assert BRANCH_ID not in codes

    def test_city_name_comes_from_the_division(self, catalog: Any) -> None:
        assert {row.city_name for row in catalog.terminals} == {"Армавир"}

    def test_pvz_and_office_are_told_apart(self, catalog: Any) -> None:
        kinds = {row.external_code: row.type for row in catalog.terminals}
        assert kinds[PVZ_WAREHOUSE] == "pvz"
        assert kinds[MAIN_WAREHOUSE] == "terminal"

    def test_full_address_wins_over_short_one(self, catalog: Any) -> None:
        # У склада два адреса: короткий для оповещений и полный. В справочник
        # идёт полный — по короткому пункт выдачи на карте не найти.
        row = next(r for r in catalog.terminals if r.external_code == MAIN_WAREHOUSE)
        assert row.address == "Россия, Краснодарский край, Армавир, улица Мичурина, 7"

    def test_coordinates_are_read(self, catalog: Any) -> None:
        row = next(r for r in catalog.terminals if r.external_code == MAIN_WAREHOUSE)
        assert (row.lat, row.lon) == (44.98426, 41.100951)

    def test_zero_limit_means_no_limit_not_zero(self, catalog: Any) -> None:
        """``0.000`` в справке — «действуют общие ограничения тарифа».

        Отдать ноль наружу значило бы объявить, что склад не принимает
        ничего, и домен перестал бы предлагать основной терминал филиала.
        """
        row = next(r for r in catalog.terminals if r.external_code == MAIN_WAREHOUSE)
        assert row.max_weight_kg is None

    def test_real_limit_is_kept(self, catalog: Any) -> None:
        row = next(r for r in catalog.terminals if r.external_code == PVZ_WAREHOUSE)
        assert row.max_weight_kg == Decimal("60.0")

    def test_work_hours_collapse_into_ranges(self, catalog: Any) -> None:
        row = next(r for r in catalog.terminals if r.external_code == MAIN_WAREHOUSE)
        assert row.work_hours == "пн-пт 09:00-18:00"

    def test_differing_days_stay_separate(self, catalog: Any) -> None:
        row = next(r for r in catalog.terminals if r.external_code == PVZ_WAREHOUSE)
        assert row.work_hours == "пн-вт 10:00-19:00, сб 10:00-16:00"


class TestCities:
    def test_city_without_divisions_is_skipped(self, catalog: Any) -> None:
        # «Успенское» есть в ответе, но отделений в нём нет. Код, который ПЭК
        # не примет, в справочнике хуже отсутствующего.
        assert {city.name for city in catalog.cities} == {"Армавир"}

    def test_code_is_a_warehouse_not_a_city_id(self, catalog: Any) -> None:
        city = catalog.cities[0]
        assert city.code == MAIN_WAREHOUSE
        assert city.code != CITY_ID

    def test_main_office_represents_the_city_over_a_pvz(self, catalog: Any) -> None:
        # У Армавира два склада: основное отделение филиала и ПВЗ. По умолчанию
        # домен подставит представителя в расчёт, и до отделения доезжает
        # больше грузов, чем до пункта выдачи.
        assert catalog.cities[0].code == MAIN_WAREHOUSE

    def test_terminals_count_is_the_city_network(self, catalog: Any) -> None:
        assert catalog.cities[0].terminals_count == 2

    def test_city_of_a_closing_division_is_skipped(self, catalog: Any) -> None:
        # Москва в фикстуре есть, но её единственное отделение без складов.
        assert "Москва" not in {city.name for city in catalog.cities}


class TestRefusalsAndEdges:
    def test_empty_body_is_an_empty_catalog(self) -> None:
        """Пусто — это состояние выгрузки, а не ошибка.

        Гасить ли сеть на пустом ответе, решает домен (ADR-0009); адаптеру
        падать здесь нечем.
        """
        catalog = parse_branches({})
        assert catalog.cities == ()
        assert catalog.terminals == ()

    def test_garbage_rows_do_not_break_the_sync(self) -> None:
        body = {"branches": ["не объект", None, {"divisions": "не список"}]}
        catalog = parse_branches(body)
        assert catalog.terminals == ()

    def test_a_full_dump_is_complete(self, catalog: Any) -> None:
        # ``/branches/all/`` отдаёт всю сеть, поэтому домен вправе гасить
        # отсутствующие терминалы. Частичной выгрузки у этого метода нет.
        assert catalog.is_complete is True


@pytest.mark.asyncio
class TestAdapterCall:
    """Что уходит к перевозчику. Разбор проверен выше, здесь — сам вызов."""

    async def test_full_network_is_asked_for(self) -> None:
        """Тело пустое намеренно: у метода параметры — это ФИЛЬТРЫ.

        Передать любой из них значило бы выгрузить кусок сети и молча
        погасить всё остальное: домен на полной выгрузке гасит отсутствующие
        терминалы.
        """
        seen: list[tuple[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(
                200, json=json.loads((FIXTURES / "branches_all.json").read_text(encoding="utf-8"))
            )

        def factory(_: CarrierAccount) -> PecomClient:
            inner = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL
            )
            return PecomClient(login="user", api_key="key", http_client=inner)

        account = CarrierAccount(
            account_id="acc-1",
            carrier_code="pecom",
            mode="own_contract",
            credentials={"login": "user", "api_key": "key"},
        )
        result = await PecomAdapter(client_factory=factory).fetch_refs(account)

        assert len(seen) == 1
        path, body = seen[0]
        assert path.endswith(BRANCHES_PATH)
        assert body == {}
        assert len(result.terminals) == 2
