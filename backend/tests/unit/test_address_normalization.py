"""Нормализация адресов и вычисление ключа города (FR-8.1).

Тесты построены от краевых случаев, а не от удачного: ТЗ называет сопоставление
городов источником большинства ошибок в мультиперевозочных системах, и все
перечисленные ниже случаи взяты из официального справочника городов ДаData
(1117 строк), где у 14 городов колонка ``city`` пуста.
"""

from __future__ import annotations

import pytest

from aerogram.directories.normalization import (
    AddressFitness,
    FitnessBlocker,
    assess_fitness,
    city_kladr_id,
    parse_level,
    resolve_city_key,
)
from aerogram.directories.schemas import DadataAddressData

#: Идентификаторы ФИАС городов федерального значения — из справочника hflabs/city.
MOSCOW = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
SPB = "c2deb16a-0330-4f05-821f-1d09c93331e6"
SEVASTOPOL = "6fdecb78-893a-4e3f-a5ba-aa062459463b"


def _data(**kwargs: str | None) -> DadataAddressData:
    """Ответ ДаData с указанными полями, остальные пустые."""
    return DadataAddressData(country_iso_code="RU", **kwargs)


class TestOrdinaryCity:
    """Обычный город: 1102 случая из 1117."""

    def test_novosibirsk_resolves_to_city_level(self) -> None:
        key = resolve_city_key(
            _data(
                region="Новосибирская",
                region_with_type="Новосибирская обл",
                region_fias_id="1ac46b49-3209-4814-b7bf-a509ea1aecd9",
                region_kladr_id="5400000000000",
                city="Новосибирск",
                city_with_type="г Новосибирск",
                city_fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                city_kladr_id="5400000100000",
                fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                fias_level="4",
                kladr_id="5400000100000",
            )
        )
        assert key is not None
        assert key.fias_id == "8dea00e3-9aab-4d8e-887c-ef2aaa546456"
        assert key.fias_level == 4
        assert key.name == "Новосибирск"

    def test_region_is_not_a_parent_for_ordinary_city(self) -> None:
        """«Новосибирская область» не пункт доставки и в родители не годится.

        Откат сопоставления на область дал бы перевозчику бессмысленный
        ориентир вместо честного «города нет в справочнике».
        """
        key = resolve_city_key(
            _data(
                region_fias_id="1ac46b49-3209-4814-b7bf-a509ea1aecd9",
                region_kladr_id="5400000000000",
                region="Новосибирская",
                city="Новосибирск",
                city_fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                fias_level="4",
            )
        )
        assert key is not None
        assert key.parent_fias_id is None


class TestFederalCities:
    """Москва, Санкт-Петербург и Севастополь: ФИАС не знает у них города."""

    @pytest.mark.parametrize(
        ("fias", "kladr", "name"),
        [
            (MOSCOW, "7700000000000", "Москва"),
            (SPB, "7800000000000", "Санкт-Петербург"),
            (SEVASTOPOL, "9200000000000", "Севастополь"),
        ],
    )
    def test_key_comes_from_region_level(self, fias: str, kladr: str, name: str) -> None:
        key = resolve_city_key(
            _data(
                region=name,
                region_with_type=f"г {name}",
                region_fias_id=fias,
                region_kladr_id=kladr,
                fias_id=fias,
                fias_level="1",
                kladr_id=kladr,
            )
        )
        assert key is not None
        assert key.fias_id == fias
        assert key.fias_level == 1
        assert key.name == name
        # Регион и есть город: сам себе родителем он быть не может.
        assert key.parent_fias_id is None


class TestMoscowOblastCitiesAtAreaLevel:
    """Десять городов Подмосковья лежат на уровне района, ``city`` у них пуст."""

    @pytest.mark.parametrize("name", ["Одинцово", "Ногинск", "Сергиев Посад", "Клин"])
    def test_area_wins_the_ladder(self, name: str) -> None:
        key = resolve_city_key(
            _data(
                region="Московская",
                region_with_type="Московская обл",
                region_fias_id="29251dcf-00a1-4e34-98d4-5c47484a36d4",
                region_kladr_id="5000000000000",
                area=name,
                area_with_type=f"{name} р-н",
                area_fias_id="11111111-2222-3333-4444-555555555555",
                fias_id="11111111-2222-3333-4444-555555555555",
                fias_level="3",
            )
        )
        assert key is not None
        assert key.fias_level == 3
        assert key.name == name
        # Московская область городом не является — в родители не идёт.
        assert key.parent_fias_id is None


class TestSettlements:
    """Населённый пункт — отдельный пункт назначения, а не «часть города»."""

    def test_alupka_keeps_its_own_key_with_yalta_as_parent(self) -> None:
        key = resolve_city_key(
            _data(
                region="Крым",
                region_with_type="Респ Крым",
                region_fias_id="6fdecb78-893a-4e3f-a5ba-aa062459463c",
                region_kladr_id="9100000000000",
                city="Ялта",
                city_with_type="г Ялта",
                city_fias_id="bbbbbbbb-1111-2222-3333-444444444444",
                city_kladr_id="9100000300000",
                settlement="Алупка",
                settlement_with_type="г Алупка",
                settlement_fias_id="daa6815b-0cf0-44c7-981c-84d72d51f2b1",
                fias_id="daa6815b-0cf0-44c7-981c-84d72d51f2b1",
                fias_level="6",
            )
        )
        assert key is not None
        assert key.fias_id == "daa6815b-0cf0-44c7-981c-84d72d51f2b1"
        assert key.fias_level == 6
        assert key.name == "Алупка"
        # Ялта — следующий элемент лестницы, годится для управляемого отката.
        assert key.parent_fias_id == "bbbbbbbb-1111-2222-3333-444444444444"

    def test_settlement_does_not_inherit_parent_kladr(self) -> None:
        """КЛАДР берётся у выигравшего объекта, иначе Алупка «станет» Ялтой.

        Если бы КЛАДР пришёл от Ялты, автосопоставление по префиксу связало бы
        код Ялты у перевозчика со строкой Алупки, и все отправления в Алупку
        уехали бы в Ялту.
        """
        key = resolve_city_key(
            _data(
                city="Ялта",
                city_fias_id="bbbbbbbb-1111-2222-3333-444444444444",
                city_kladr_id="9100000300000",
                settlement="Алупка",
                settlement_fias_id="daa6815b-0cf0-44c7-981c-84d72d51f2b1",
                settlement_kladr_id=None,
                fias_id="daa6815b-0cf0-44c7-981c-84d72d51f2b1",
                fias_level="6",
                kladr_id="9100000300000",
            )
        )
        assert key is not None
        assert key.kladr_id != "9100000300000"
        assert key.kladr_id is None


class TestCitiesInsideMoscow:
    """Зеленоград и Троицк — самостоятельные направления внутри региона Москва."""

    def test_zelenograd_keeps_own_key_with_moscow_as_parent(self) -> None:
        key = resolve_city_key(
            _data(
                region="Москва",
                region_with_type="г Москва",
                region_fias_id=MOSCOW,
                region_kladr_id="7700000000000",
                city="Зеленоград",
                city_with_type="г Зеленоград",
                city_fias_id="cccccccc-1111-2222-3333-444444444444",
                city_kladr_id="7700000200000",
                fias_id="cccccccc-1111-2222-3333-444444444444",
                fias_level="4",
            )
        )
        assert key is not None
        assert key.fias_id == "cccccccc-1111-2222-3333-444444444444"
        # Город федерального значения — единственный случай, когда регион
        # становится родителем: у Зеленограда откат на Москву осмыслен.
        assert key.parent_fias_id == MOSCOW


class TestFullAddressStandardisation:
    """Стандартизация полного адреса: fias_id — это ДОМ, а не город."""

    def test_house_guid_never_becomes_the_city_key(self) -> None:
        key = resolve_city_key(
            _data(
                region="Москва",
                region_with_type="г Москва",
                region_fias_id=MOSCOW,
                region_kladr_id="7700000000000",
                street="Сухонская",
                street_with_type="ул Сухонская",
                house="11",
                # Самый глубокий объект — дом, и его идентификатор не город.
                fias_id="5ee84ac0-eb9a-4b42-b814-2f5f7c27c255",
                fias_level="8",
                kladr_id="77000000000283600",
            )
        )
        assert key is not None
        assert key.fias_id == MOSCOW
        assert key.fias_level == 1

    def test_full_name_never_contains_street_or_house(self) -> None:
        """Утечка ПДн: cities — платформенная таблица, RLS на неё не действует.

        Если в наименование города попадёт улица, дом и квартира получателя,
        адрес одного тенанта станет виден всем остальным.
        """
        key = resolve_city_key(
            _data(
                region="Москва",
                region_with_type="г Москва",
                region_fias_id=MOSCOW,
                region_kladr_id="7700000000000",
                street_with_type="ул Сухонская",
                house="11",
                flat="89",
                fias_id="5ee84ac0-eb9a-4b42-b814-2f5f7c27c255",
                fias_level="8",
            )
        )
        assert key is not None
        assert "Сухонская" not in key.full_name
        assert "11" not in key.full_name
        assert "89" not in key.full_name
        assert key.full_name == "г Москва"


class TestRejection:
    def test_foreign_address_is_rejected(self) -> None:
        # locations по стране — условие необходимое, но не достаточное:
        # ДаData вернёт зарубежный город, если по РФ ничего не нашлось.
        data = DadataAddressData(country_iso_code="KZ", city="Алматы", city_fias_id="x")
        assert resolve_city_key(data) is None

    def test_address_without_any_locality_is_rejected(self) -> None:
        assert resolve_city_key(_data(street="Ленина", house="1")) is None

    def test_empty_strings_are_treated_as_missing(self) -> None:
        # ДаData отдаёт пустую строку там же, где мог бы отдать null.
        key = resolve_city_key(
            _data(
                settlement_fias_id="   ",
                city_fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                city="Новосибирск",
                fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                fias_level="4",
            )
        )
        assert key is not None
        assert key.fias_id == "8dea00e3-9aab-4d8e-887c-ef2aaa546456"


class TestKladrNormalisation:
    def test_street_code_is_truncated_to_locality(self) -> None:
        assert city_kladr_id("77000000000283600") == "7700000000000"

    def test_locality_code_is_kept_as_is(self) -> None:
        assert city_kladr_id("5400000100000") == "5400000100000"

    def test_region_code_is_not_a_locality(self) -> None:
        # Код короче 13 знаков — это регион или район, городом он не является.
        assert city_kladr_id("54000") is None

    @pytest.mark.parametrize("value", [None, "", "   ", "не-число"])
    def test_garbage_gives_none(self, value: str | None) -> None:
        assert city_kladr_id(value) is None


class TestParseLevel:
    @pytest.mark.parametrize(("raw", "expected"), [("4", 4), ("65", 65), ("-1", -1), (8, 8)])
    def test_parses_known_forms(self, raw: str | int, expected: int) -> None:
        assert parse_level(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "город"])
    def test_unparsable_gives_none(self, raw: str | None) -> None:
        assert parse_level(raw) is None


class TestFitness:
    """Пригодность адреса: «для чего годится», а не «валиден ли»."""

    def test_city_with_manually_typed_house_is_fit_for_door_delivery(self) -> None:
        """Уровень объекта ФИАС и наличие дома — разные величины.

        Оператор выбирает город из подсказки (уровень 4) и дописывает дом руками.
        Если считать «дом есть» только на уровнях 8-9, такой адрес навсегда
        останется непригодным для доставки до двери, хотя дом в нём указан.
        """
        data = _data(
            region_with_type="Новосибирская обл",
            region_fias_id="1ac46b49-3209-4814-b7bf-a509ea1aecd9",
            city="Новосибирск",
            city_with_type="г Новосибирск",
            city_fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
            fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
            fias_level="4",
            street="Ленина",
            house="12",
            flat="5",
        )
        fitness, blockers = assess_fitness(data, resolve_city_key(data))
        assert fitness is AddressFitness.DOOR
        assert blockers == []

    def test_city_without_house_is_fit_for_pickup_point_only(self) -> None:
        data = _data(
            city="Новосибирск",
            city_with_type="г Новосибирск",
            city_fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
            fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
            fias_level="4",
        )
        fitness, blockers = assess_fitness(data, resolve_city_key(data))
        assert fitness is AddressFitness.LOCALITY
        assert FitnessBlocker.NO_HOUSE in blockers

    def test_postal_box_is_never_fit_for_courier_delivery(self) -> None:
        data = _data(
            city="Новосибирск",
            city_with_type="г Новосибирск",
            city_fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
            fias_id="8dea00e3-9aab-4d8e-887c-ef2aaa546456",
            fias_level="4",
            postal_box="а/я 15",
            house="15",
        )
        fitness, blockers = assess_fitness(data, resolve_city_key(data))
        assert fitness is AddressFitness.LOCALITY
        assert FitnessBlocker.POSTAL_BOX in blockers

    def test_address_without_city_is_unusable(self) -> None:
        data = _data(street="Ленина", house="1")
        fitness, blockers = assess_fitness(data, resolve_city_key(data))
        assert fitness is AddressFitness.UNUSABLE
        assert FitnessBlocker.NO_CITY in blockers

    def test_foreign_address_is_unusable(self) -> None:
        data = DadataAddressData(country_iso_code="KZ", city="Алматы")
        fitness, blockers = assess_fitness(data, None)
        assert fitness is AddressFitness.UNUSABLE
        assert FitnessBlocker.FOREIGN_COUNTRY in blockers
