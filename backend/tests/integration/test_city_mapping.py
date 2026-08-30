"""Сопоставление городов с кодами перевозчиков (FR-8.2, FR-12.3).

Проверяется на поддельном справочнике перевозчика: настоящих адаптеров ещё нет,
а интерфейс обязан быть готов и проверен до их появления.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.carriers.base import CarrierCity
from aerogram.directories.models import Carrier, City
from aerogram.directories.repository import CityMappingRepository
from aerogram.directories.service import CityMappingService
from aerogram.shared.ids import uuid7

pytestmark = pytest.mark.integration

MOSCOW = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
YALTA = "bbbbbbbb-1111-2222-3333-444444444444"
ALUPKA = "daa6815b-0cf0-44c7-981c-84d72d51f2b1"


@pytest.fixture
async def carrier(migrator_session: AsyncSession) -> AsyncIterator[Carrier]:
    """Перевозчик и наполненный справочник городов."""
    db = migrator_session
    row = Carrier(id=uuid7(), code="fake", name="Поддельный перевозчик")
    db.add(row)
    db.add_all(
        [
            City(
                id=uuid7(),
                fias_id=MOSCOW,
                name="Москва",
                full_name="г Москва",
                fias_level=1,
                region="Москва",
                kladr_id="7700000000000",
            ),
            City(
                id=uuid7(),
                fias_id=YALTA,
                name="Ялта",
                full_name="Респ Крым, г Ялта",
                fias_level=4,
                region="Крым",
                kladr_id="9100000300000",
            ),
            City(
                id=uuid7(),
                fias_id=ALUPKA,
                name="Алупка",
                full_name="Респ Крым, г Ялта, г Алупка",
                fias_level=6,
                parent_fias_id=YALTA,
                region="Крым",
            ),
            # Одноимённые населённые пункты — норма для России.
            City(id=uuid7(), fias_id="ivanovka-1", name="Ивановка", fias_level=6, region="Курская"),
            City(id=uuid7(), fias_id="ivanovka-2", name="Ивановка", fias_level=6, region="Омская"),
        ]
    )
    await db.flush()
    yield row
    await db.rollback()


class TestAutoMatching:
    async def test_carrier_supplied_fias_wins_immediately(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        service = CityMappingService(migrator_session)
        counters = await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="MSK", name="Москва", fias_id=MOSCOW)]
        )

        assert counters["fias"] == 1
        assert await service.resolve(carrier.id, MOSCOW) == "MSK"

    async def test_fias_match_is_confirmed_automatically(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="MSK", name="Москва", fias_id=MOSCOW)]
        )

        mapping = await CityMappingRepository(migrator_session).resolve(carrier.id, MOSCOW)
        assert mapping is not None
        assert mapping.is_confirmed is True
        assert mapping.match_method == "fias"

    async def test_exact_name_with_region_is_matched(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="YLT", name="Ялта", region="Крым")]
        )

        assert await service.resolve(carrier.id, YALTA) == "YLT"

    async def test_homonyms_without_region_go_to_the_queue(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        """Имя без региона не определяет пункт назначения.

        Двух «Ивановок» алгоритм не различит никогда, а человек различит
        за секунду — ради этого и существует очередь ручного сопоставления.
        """
        service = CityMappingService(migrator_session)
        counters = await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="IVA", name="Ивановка")]
        )

        assert counters["queued"] == 1
        queue = await CityMappingRepository(migrator_session).list_open(carrier.id)
        assert len(queue) == 1
        assert queue[0].reason == "ambiguous"
        assert len(queue[0].candidates) >= 2

    async def test_wrong_region_vetoes_the_match(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        """Регион работает как вето, а не как надбавка к оценке."""
        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="YLT", name="Ялта", region="Московская")]
        )

        assert await service.resolve(carrier.id, YALTA) is None

    async def test_unknown_city_goes_to_the_queue_with_reason(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        service = CityMappingService(migrator_session)
        counters = await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="XXX", name="Урюпинск-Заречный", terminals_count=7)]
        )

        assert counters["queued"] == 1
        queue = await CityMappingRepository(migrator_session).list_open(carrier.id)
        assert queue[0].reason == "no_match"
        # Приоритет разбора: где больше терминалов, там дороже простой.
        assert queue[0].terminals_count == 7


class TestManualDecisionWins:
    async def test_sync_never_overwrites_confirmed_mapping(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        """Человеческое решение старше машинного.

        Ночная задача, молча меняющая подтверждённый код города, — это ошибка,
        которая обнаруживается по жалобе клиента через сутки.
        """
        repo = CityMappingRepository(migrator_session)
        await repo.upsert(
            carrier_id=carrier.id,
            city_fias_id=MOSCOW,
            carrier_city_code="MSK-MANUAL",
            carrier_city_name="Москва",
            match_method="manual",
            match_score=1.0,
            is_confirmed=True,
        )

        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="MSK-AUTO", name="Москва", fias_id=MOSCOW)]
        )

        assert await service.resolve(carrier.id, MOSCOW) == "MSK-MANUAL"

    async def test_unconfirmed_mapping_is_updated_by_sync(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        repo = CityMappingRepository(migrator_session)
        await repo.upsert(
            carrier_id=carrier.id,
            city_fias_id=MOSCOW,
            carrier_city_code="OLD",
            carrier_city_name="Москва",
            match_method="fuzzy_name",
            match_score=0.94,
            is_confirmed=False,
        )

        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="NEW", name="Москва", fias_id=MOSCOW)]
        )

        assert await service.resolve(carrier.id, MOSCOW) == "NEW"

    async def test_manual_confirmation_closes_the_queue_item(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(carrier.id, [CarrierCity(code="IVA", name="Ивановка")])
        repo = CityMappingRepository(migrator_session)
        item = (await repo.list_open(carrier.id))[0]

        await service.confirm(item.id, "ivanovka-2", user_id=None)

        assert await service.resolve(carrier.id, "ivanovka-2") == "IVA"
        assert await repo.list_open(carrier.id) == []


class TestResolution:
    async def test_missing_mapping_is_none_not_an_exception(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        """Отсутствие сопоставления — не исключение.

        На пути расчёта оно даёт отдельную строку выдачи, и перевозчик
        не вызывается вообще: три секунды таймаута из общего дедлайна
        в пять секунд экономятся (FR-1.3, FR-1.4).
        """
        service = CityMappingService(migrator_session)
        assert await service.resolve(carrier.id, MOSCOW) is None

    async def test_falls_back_to_parent_city(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        """Для Алупки родитель — Ялта: откат помечается, а не выполняется молча."""
        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="YLT", name="Ялта", region="Крым")]
        )

        code, is_fallback = await service.resolve_with_fallback(carrier.id, ALUPKA)
        assert code == "YLT"
        assert is_fallback is True

    async def test_direct_match_is_not_marked_as_fallback(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        service = CityMappingService(migrator_session)
        await service.match_carrier_cities(
            carrier.id, [CarrierCity(code="MSK", name="Москва", fias_id=MOSCOW)]
        )

        code, is_fallback = await service.resolve_with_fallback(carrier.id, MOSCOW)
        assert code == "MSK"
        assert is_fallback is False

    async def test_city_without_parent_has_nothing_to_fall_back_to(
        self, migrator_session: AsyncSession, carrier: Carrier
    ) -> None:
        service = CityMappingService(migrator_session)
        code, is_fallback = await service.resolve_with_fallback(carrier.id, YALTA)
        assert code is None
        assert is_fallback is False
