"""Нормализация адресов: вычисление ключа города и пригодности адреса.

Чистые функции без ввода-вывода: их можно проверить на краевых случаях без сети
и без БД, а именно на краевых случаях эта тема и ломается. ТЗ прямо называет
сопоставление городов «источником большинства ошибок в мультиперевозочных
системах» (раздел 5.1) — поэтому здесь всё построено от ошибок, а не от
удачного случая.

Главное правило: **ключ города никогда не берётся из одного поля ДаData**.
``data.fias_id`` — это идентификатор самого глубокого найденного объекта.
В подсказке города он равен нужному значению, а в стандартизации полного
адреса это идентификатор ДОМА. Запись такого значения в
``addresses.city_fias_id`` заполнила бы ``city_carrier_map`` мусором, который
никогда ни с чем не сопоставится.
"""

from __future__ import annotations

from dataclasses import dataclass

from aerogram.directories.schemas import DadataAddressData
from aerogram.shared.addresses import AddressFitness, FitnessBlocker
from aerogram.shared.addresses import assess_fitness as assess_fields

__all__ = [
    "CITY_LEVELS",
    "FEDERAL_CITY_KLADR",
    "AddressFitness",
    "CityKey",
    "FitnessBlocker",
    "assess_fitness",
    "city_kladr_id",
    "parse_level",
    "resolve_city_key",
]

#: Уровни ФИАС, на которых объект является пунктом доставки.
#: 1 — регион (города федерального значения), 3 — район (десять городов
#: Подмосковья), 4 — город, 6 — населённый пункт, 65 — планировочная структура.
CITY_LEVELS: frozenset[int] = frozenset({1, 3, 4, 6, 65})

#: КЛАДР-коды регионов, которые сами являются городами. Только для них регион
#: имеет смысл в качестве родителя: для Зеленограда родитель — Москва, а для
#: Новосибирска «Новосибирская область» пунктом доставки не является.
FEDERAL_CITY_KLADR: frozenset[str] = frozenset({"7700000000000", "7800000000000", "9200000000000"})

#: Значащая часть кода КЛАДР населённого пункта: регион(2) + район(3) + город(3)
#: + населённый пункт(3). Дальше идут улица(4) и дом(4).
_KLADR_LOCALITY_DIGITS = 11
#: Полный код населённого пункта — значащая часть плюс два знака актуальности.
_KLADR_ACTUALITY_SUFFIX = "00"


@dataclass(frozen=True, slots=True)
class CityKey:
    """Ключ города и его окружение, вычисленные по лестнице.

    ``parent_fias_id`` — следующий элемент той же лестницы. Он нужен
    сопоставлению с перевозчиком: если своего кода у посёлка нет, откат
    на родителя даёт рабочий результат с явной отметкой отката, а не тишину.
    """

    fias_id: str
    parent_fias_id: str | None
    fias_level: int
    region_fias_id: str | None
    kladr_id: str | None
    name: str
    full_name: str


def _clean(value: str | None) -> str | None:
    """Пустая строка от ДаData равнозначна отсутствию значения."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_level(value: str | int | None) -> int | None:
    """Разобрать fias_level. ДаData отдаёт его строкой, иногда со знаком минус."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def city_kladr_id(value: str | None) -> str | None:
    """Привести КЛАДР к 13-значному коду населённого пункта.

    У перевозчиков в справочниках лежит код города, а не улицы и не дома.
    Простым срезом это не делается: код населённого пункта — это 11 значащих
    знаков плюс два знака актуальности, а срез ``[:13]`` от кода улицы
    ``77000000000283600`` дал бы ``7700000000028``, то есть залез бы в номер
    улицы и создал несуществующий населённый пункт.

    Более короткий код — это регион или район, городом он не является.
    """
    cleaned = _clean(value)
    if cleaned is None or not cleaned.isdigit():
        return None
    if len(cleaned) < _KLADR_LOCALITY_DIGITS + len(_KLADR_ACTUALITY_SUFFIX):
        return None
    return cleaned[:_KLADR_LOCALITY_DIGITS] + _KLADR_ACTUALITY_SUFFIX


def _dedup(values: tuple[str | None, ...]) -> list[str]:
    """Убрать пустые и повторы, сохранив порядок.

    Повторы реальны: у Москвы region_fias_id и city_fias_id могут совпасть,
    и родителем города не должен оказаться он сам.
    """
    seen: set[str] = set()
    chain: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            chain.append(value)
    return chain


def resolve_city_key(data: DadataAddressData) -> CityKey | None:
    """Вычислить ключ города из ответа ДаData.

    Возвращает ``None``, если населённый пункт определить не удалось: это
    не ошибка функции, а состояние адреса, и решать, что с ним делать,
    обязан вызывающий слой.

    Лестница ``settlement → city → area → region`` и её порядок обсуждению
    не подлежат:

    * ``settlement`` первым — для перевозчика посёлок и город, в округ которого
      он входит, разные пункты назначения с разными тарифами и сроками;
    * ``city`` — обычный случай, 1102 города из 1117 официального справочника;
    * ``area`` — десять городов Подмосковья, у них ``fias_level = 3``
      и пустой ``city`` (Одинцово, Ногинск, Сергиев Посад и другие);
    * ``region`` — Москва, Санкт-Петербург и Севастополь: ФИАС не знает у них
      города вовсе, объект живёт на уровне региона.

    Район города (уровень 5) в лестницу не входит: «р-н Северное Медведково»
    пунктом доставки не является.
    """
    country = _clean(data.country_iso_code)
    if country is not None and country != "RU":
        # Ограничение locations по стране — условие необходимое, но не
        # достаточное: подсказки возвращают зарубежный город, если по РФ
        # ничего не нашлось.
        return None

    settlement_fias = _clean(data.settlement_fias_id)
    city_fias = _clean(data.city_fias_id)
    area_fias = _clean(data.area_fias_id)
    region_fias = _clean(data.region_fias_id)

    chain = _dedup((settlement_fias, city_fias, area_fias, region_fias))
    if not chain:
        return None

    primary = chain[0]
    parent = chain[1] if len(chain) > 1 else None

    # Регион годится в родители, только если он сам город федерального значения.
    if (
        parent is not None
        and parent == region_fias
        and city_kladr_id(data.region_kladr_id) not in FEDERAL_CITY_KLADR
    ):
        parent = None

    level = _resolve_level(data, primary, settlement_fias, city_fias, area_fias)
    name = _first_non_empty(
        _clean(data.settlement),
        _clean(data.city),
        _clean(data.area),
        _clean(data.region),
    )
    if name is None:
        return None

    return CityKey(
        fias_id=primary,
        parent_fias_id=parent,
        fias_level=level,
        region_fias_id=region_fias,
        kladr_id=_ladder_kladr(data, primary, settlement_fias, city_fias),
        name=name,
        full_name=city_full_name(data),
    )


def _resolve_level(
    data: DadataAddressData,
    primary: str,
    settlement_fias: str | None,
    city_fias: str | None,
    area_fias: str | None,
) -> int:
    """Уровень ФИАС ИМЕННО выигравшего объекта, а не всей подсказки.

    Если выигравший объект и есть самый глубокий (случай подсказки города),
    уровень берётся у ДаData как есть — так сохраняются 1, 3 и 65.
    Иначе (случай полного адреса, где самый глубокий объект — дом) уровень
    выводится из выигравшего слота лестницы.
    """
    if _clean(data.fias_id) == primary:
        level = parse_level(data.fias_level)
        if level is not None:
            return level
    if primary == settlement_fias:
        return 6
    if primary == city_fias:
        return 4
    if primary == area_fias:
        return 3
    return 1


def _ladder_kladr(
    data: DadataAddressData, primary: str, settlement_fias: str | None, city_fias: str | None
) -> str | None:
    """КЛАДР того же объекта, что выиграл лестницу ключа.

    Порядок обязан совпадать с лестницей. Иначе для «Респ Крым, г Ялта,
    г Алупка» ключом станет ФИАС Алупки, а КЛАДР — вышестоящей Ялты, и
    автосопоставление по префиксу КЛАДР свяжет код Ялты у перевозчика
    со строкой Алупки: все отправления в Алупку уедут в Ялту.

    Если у выигравшего объекта своего КЛАДР нет, возвращается ``None``:
    отсутствие кода честнее, чем код родителя.
    """
    if primary == settlement_fias:
        return city_kladr_id(data.settlement_kladr_id)
    if primary == city_fias:
        return city_kladr_id(data.city_kladr_id)
    return city_kladr_id(data.kladr_id)


def city_full_name(data: DadataAddressData) -> str:
    """Читаемое наименование населённого пункта.

    Собирается ТОЛЬКО из полей городского уровня. Брать ``value`` или
    ``unrestricted_value`` нельзя: в стандартизации полного адреса там лежит
    улица, дом и квартира получателя, то есть персональные данные, а таблица
    ``cities`` общая для всех тенантов и под RLS не попадает (12.1, 12.7 ТЗ).
    """
    parts = [
        _clean(data.region_with_type),
        _clean(data.area_with_type),
        _clean(data.city_with_type),
        _clean(data.settlement_with_type),
    ]
    named = [p for p in parts if p]
    if named:
        return ", ".join(dict.fromkeys(named))
    fallback = _first_non_empty(
        _clean(data.settlement),
        _clean(data.city),
        _clean(data.area),
        _clean(data.region),
    )
    return fallback or ""


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def assess_fitness(
    data: DadataAddressData, city: CityKey | None
) -> tuple[AddressFitness, list[FitnessBlocker]]:
    """Оценить пригодность адреса, разобранного ДаData.

    Обёртка над общим правилом из ``shared.addresses``: то же самое правило
    применяется к адресу, введённому руками в адресной книге, и расходиться
    эти два ответа не имеют права.
    """
    country = _clean(data.country_iso_code)
    return assess_fields(
        city_known=city is not None,
        house_known=_clean(data.house) is not None,
        postal_box=_clean(data.postal_box) is not None,
        foreign=country is not None and country != "RU",
    )
