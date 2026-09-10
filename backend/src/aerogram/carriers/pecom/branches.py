"""Справочник подразделений ПЭК: разбор ответа ``/branches/all/``. Без ввода-вывода.

**Источник** — официальная справка ПЭК, раздел «Операции с филиалами»
(`docs/integrations/sources/pecom/help_branches.html`, выкачана человеком,
ADR-0020). Машинной спецификации у ПЭК нет, поэтому контракт описан здесь
по этой справке, а фикстуры остаются синтетическими и помеченными.

**Главная ловушка, и справка кричит о ней капслоком.** В ответе четыре разных
идентификатора, и в расчёте стоимости годится ровно один:

* ``branches[].id`` — филиал, **НЕ ИСПОЛЬЗОВАТЬ**;
* ``branches[].cities[].cityId`` — город, **НЕ ИСПОЛЬЗОВАТЬ**;
* ``branches[].divisions[].id`` — отделение, **НЕ ИСПОЛЬЗОВАТЬ**;
* ``branches[].divisions[].warehouses[].id`` — склад, **и только он** идёт
  в ``senderWarehouseId``/``receiverWarehouseId``.

Поэтому ``CarrierCity.code`` здесь — идентификатор СКЛАДА, а не города:
домен кладёт его в ``city_carrier_map`` и потом передаёт адаптеру как
``carrier_city_code``, а тот подставляет его в расчёт (``quotes._warehouse_id``).
Положить сюда ``cityId`` значило бы наполнить справочник значениями, которые
ПЭК отвергает, — и узнать об этом на первом же расчёте у клиента.

**Две структуры, и они не совпадают.** Справка различает географическую
(«какой филиал обслуживает какие города» — ``cities[].divisions[]``)
и финансовую (``divisions[]`` с их складами). Город берёт свои отделения
из первой, склад свой город — из второй, через ``division.cityId``.
Смешивать их нельзя: у одного филиала эти списки разные.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final, Literal

from aerogram.carriers.base import CarrierCity, CarrierTerminalRow, RefCatalog

__all__ = ["BRANCHES_PATH", "parse_branches"]

BRANCHES_PATH: Final = "/branches/all/"

#: ``departmentTypeId`` из справки: 0 — отделение компании, 1 — ПВЗ,
#: 4 — основное отделение компании в филиале. Всё, что не ПВЗ, для нас
#: терминал: разница между обычным и основным отделением — про бухгалтерию,
#: а не про то, куда приезжает груз.
_PVZ_TYPE: Final = 1
_MAIN_OFFICE_TYPE: Final = 4
_COMPANY_OFFICE_TYPE: Final = 0

#: Порядок предпочтения представителя города. Основное отделение филиала
#: впереди, ПВЗ позади: до кода города, который домен подставит в расчёт
#: по умолчанию, доезжает больше грузов, чем до пункта выдачи.
_REPRESENTATIVE_ORDER: Final = {
    _MAIN_OFFICE_TYPE: 0,
    _COMPANY_OFFICE_TYPE: 1,
    _PVZ_TYPE: 2,
}

#: Дни недели в ``divisionTimeOfWork``: 1 — понедельник, 6 — суббота.
_WEEKDAYS: Final = {"1": "пн", "2": "вт", "3": "ср", "4": "чт", "5": "пт", "6": "сб", "7": "вс"}


def parse_branches(body: dict[str, Any]) -> RefCatalog:
    """Ответ ``/branches/all/`` → справочники для домена.

    Пустой ответ даёт пустой каталог, а не исключение: «филиалов не пришло» —
    это состояние выгрузки, и решать, гасить ли сеть, домену (ADR-0009).
    """
    branches = body.get("branches")
    if not isinstance(branches, list):
        return RefCatalog()

    terminals: list[CarrierTerminalRow] = []
    cities: list[CarrierCity] = []

    for branch in branches:
        if not isinstance(branch, dict):
            continue
        divisions = _divisions_by_id(branch)
        city_names = _city_names_by_id(branch)
        terminals.extend(_terminals_of(divisions.values(), city_names))
        cities.extend(_cities_of(branch, divisions))

    return RefCatalog(cities=tuple(cities), terminals=tuple(terminals))


def _divisions_by_id(branch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Финансовая структура филиала: отделения по идентификатору."""
    rows = branch.get("divisions")
    if not isinstance(rows, list):
        return {}
    return {
        str(row["id"]): row for row in rows if isinstance(row, dict) and row.get("id") is not None
    }


def _city_names_by_id(branch: dict[str, Any]) -> dict[str, str]:
    """Названия городов филиала по идентификатору."""
    rows = branch.get("cities")
    if not isinstance(rows, list):
        return {}
    names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        city_id, title = row.get("cityId"), row.get("title")
        if city_id is not None and title:
            names[str(city_id)] = str(title)
    return names


def _terminals_of(divisions: Any, city_names: dict[str, str]) -> list[CarrierTerminalRow]:
    """Склады отделений — по одной строке на склад.

    Справка: «Выводится один к одному. Одно отделение — один склад. Если
    массив возвращается пустым, значит отделение в ближайшее время планируется
    к закрытию». Пустой склад пропускается: терминала, в который нельзя
    привезти груз, в сети быть не должно.
    """
    rows: list[CarrierTerminalRow] = []
    for division in divisions:
        city_name = city_names.get(str(division.get("cityId") or ""))
        kind: Literal["pvz", "terminal"] = (
            "pvz" if division.get("departmentTypeId") == _PVZ_TYPE else "terminal"
        )
        hours = _work_hours(division.get("divisionTimeOfWork"))
        for warehouse in _warehouses(division):
            code = warehouse.get("id")
            if code is None:
                continue
            lat, lon = _coordinates(warehouse.get("coordinatesobj"))
            rows.append(
                CarrierTerminalRow(
                    external_code=str(code),
                    city_name=city_name,
                    address=str(warehouse.get("addressDivision") or warehouse.get("address") or "")
                    or None,
                    type=kind,
                    work_hours=hours,
                    lat=lat,
                    lon=lon,
                    max_weight_kg=_limit(warehouse.get("maxWeight")),
                )
            )
    return rows


def _cities_of(branch: dict[str, Any], divisions: dict[str, dict[str, Any]]) -> list[CarrierCity]:
    """Города филиала с кодом склада-представителя.

    Город без единого работающего склада пропускается: код, который ПЭК
    не примет, в справочнике хуже отсутствующего — на нём молча ломается
    расчёт, а не подключение.
    """
    rows = branch.get("cities")
    if not isinstance(rows, list):
        return []

    cities: list[CarrierCity] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = row.get("title")
        if not title:
            continue
        warehouses = _city_warehouses(row, divisions)
        if not warehouses:
            continue
        representative = min(warehouses, key=_representative_key)
        cities.append(
            CarrierCity(
                code=str(representative[1]["id"]),
                name=str(title),
                terminals_count=len(warehouses),
            )
        )
    return cities


def _city_warehouses(
    city: dict[str, Any], divisions: dict[str, dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Пары «отделение, склад» для города — по географической структуре."""
    ids = city.get("divisions")
    if not isinstance(ids, list):
        return []
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for division_id in ids:
        division = divisions.get(str(division_id))
        if division is None:
            continue
        pairs.extend(
            (division, warehouse)
            for warehouse in _warehouses(division)
            if warehouse.get("id") is not None
        )
    return pairs


def _representative_key(pair: tuple[dict[str, Any], dict[str, Any]]) -> tuple[int, str]:
    """Ключ выбора представителя города.

    Сначала тип отделения по документированной шкале, затем код склада —
    и второй здесь не для красоты: при равных типах порядок обязан быть
    устойчивым, иначе каждая синхронизация переписывала бы ``city_carrier_map``
    другим складом того же города, и история сопоставлений превратилась бы
    в шум.
    """
    division, warehouse = pair
    kind = division.get("departmentTypeId")
    unknown = len(_REPRESENTATIVE_ORDER)
    rank = _REPRESENTATIVE_ORDER.get(kind, unknown) if isinstance(kind, int) else unknown
    return rank, str(warehouse.get("warehouseCode") or warehouse.get("id") or "")


def _warehouses(division: dict[str, Any]) -> list[dict[str, Any]]:
    rows = division.get("warehouses")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _coordinates(raw: object) -> tuple[float | None, float | None]:
    if not isinstance(raw, dict):
        return None, None
    lat, lon = raw.get("latitude"), raw.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None, None
    return float(lat), float(lon)


def _limit(raw: object) -> Decimal | None:
    """Ограничение склада по весу.

    ``0.000`` в справке значит «действуют общие ограничения тарифа», а вовсе
    не «склад не принимает груз». Отдать ноль наружу значило бы объявить
    склад непригодным для всего.
    """
    if not isinstance(raw, (int, float, str)):
        return None
    try:
        value = Decimal(str(raw))
    except ArithmeticError:
        return None
    return value if value > 0 else None


def _work_hours(raw: object) -> str | None:
    """Часы работы одной строкой: ``пн-пт 09:00-18:00`` и подобное.

    Справка: «Если нет элемента с конкретным днем недели, значит в этот день
    отделение не работает». Дни с одинаковым интервалом сворачиваются
    в диапазон — иначе строка не помещается ни на один экран.
    """
    if not isinstance(raw, list) or not raw:
        return None

    schedule: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = _WEEKDAYS.get(str(item.get("dayOfWeek") or ""))
        if day is None:
            continue
        # Пустая строка начала дня по справке означает «00:00».
        start = str(item.get("workFrom") or "00:00")
        end = str(item.get("workTo") or "")
        if not end:
            continue
        schedule.append((day, f"{start}-{end}"))

    if not schedule:
        return None

    parts: list[str] = []
    first, hours = schedule[0]
    last = first
    for day, value in schedule[1:]:
        if value == hours:
            last = day
            continue
        parts.append(_span(first, last, hours))
        first = last = day
        hours = value
    parts.append(_span(first, last, hours))
    return ", ".join(parts)


def _span(first: str, last: str, hours: str) -> str:
    return f"{first} {hours}" if first == last else f"{first}-{last} {hours}"
