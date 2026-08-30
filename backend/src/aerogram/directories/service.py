"""Логика справочников: подсказки городов, нормализация адреса, сопоставление.

Ключевое свойство модуля — **управляемая деградация**. ДаData единственный
практичный источник ФИАС, то есть внешняя зависимость в критическом пути ввода
адреса. Раздел 3 ТЗ по реализации прямо требует, чтобы продукт не переставал
работать из-за недоступности внешнего сервиса, поэтому подсказки никогда
не отвечают ошибкой: при любом сбое ДаData выдача собирается из локального
справочника и помечается ``degraded``.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.carriers.base import CarrierCity
from aerogram.directories.dadata import DadataClient
from aerogram.directories.models import City
from aerogram.directories.normalization import (
    AddressFitness,
    CityKey,
    assess_fitness,
    city_kladr_id,
    resolve_city_key,
)
from aerogram.directories.repository import (
    CarrierRepository,
    CityMappingRepository,
    CityRepository,
    TerminalRepository,
)
from aerogram.directories.schemas import (
    CitySuggestion,
    CitySuggestResponse,
    NormalizedAddress,
    PartyDraft,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.errors import DirectoryError, NotFound
from aerogram.shared.logging import get_logger

__all__ = [
    "AMBIGUITY_MARGIN",
    "AUTO_CONFIRM_THRESHOLD",
    "FUZZY_THRESHOLD",
    "AddressService",
    "CityMappingService",
    "CityService",
    "MatchResult",
    "RefSyncReport",
]

log = get_logger(__name__)

#: Порог, ниже которого нечёткое совпадение не пишется вовсе.
FUZZY_THRESHOLD = 0.93
#: Оценка, начиная с которой сопоставление считается детерминированным
#: и подтверждается автоматически.
AUTO_CONFIRM_THRESHOLD = 0.97
#: Если второй кандидат отстаёт меньше чем на столько, сопоставление
#: не пишется: высокая оценка означает «похоже», а не «единственное».
AMBIGUITY_MARGIN = 0.05


@dataclass(frozen=True, slots=True)
class RefSyncReport:
    """Итог синхронизации справочников перевозчика.

    Отчёт составляет ДОМЕН, а не адаптер: адаптер отдаёт данные, а сколько
    строк записано, сколько городов ушло в очередь и что погашено — знает
    только тот, кто писал в базу (ADR-0009).
    """

    cities_total: int = 0
    cities_mapped: int = 0
    cities_queued: int = 0
    terminals_total: int = 0
    terminals_upserted: int = 0
    terminals_deactivated: int = 0


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Итог сопоставления одного города перевозчика с ФИАС."""

    city_fias_id: str | None
    method: str
    score: float
    is_confirmed: bool
    candidates: list[dict[str, object]]
    reason: str | None = None


class CityService:
    """Подсказки и поиск городов."""

    def __init__(self, session: AsyncSession, dadata: DadataClient | None) -> None:
        self._session = session
        self._dadata = dadata
        self._cities = CityRepository(session)

    async def suggest(self, query: str, limit: int = 10) -> CitySuggestResponse:
        """Подсказки города.

        Ответ всегда успешный. Ошибка ДаData превращается в выдачу из локального
        справочника с признаком ``degraded``: подсказка — вспомогательный сервис
        ввода, и её сбой не должен останавливать создание отправления.
        """
        if self._dadata is None:
            return await self._local(query, limit, reason="Справочник адресов не настроен")

        try:
            suggestions = await self._dadata.suggest_address(query, count=limit)
        except DirectoryError as exc:
            log.warning("directories.suggest_degraded", code=exc.code, query_length=len(query))
            return await self._local(query, limit, reason=exc.message_ru)

        items: list[CitySuggestion] = []
        for suggestion in suggestions:
            key = resolve_city_key(suggestion.data)
            if key is None:
                continue
            await self._ensure_city(key, suggestion.data.postal_code, suggestion.data.timezone)
            items.append(
                CitySuggestion(
                    fias_id=key.fias_id,
                    parent_fias_id=key.parent_fias_id,
                    fias_level=key.fias_level,
                    name=key.name,
                    full_name=key.full_name,
                    region=key.region_fias_id,
                    kladr_id=key.kladr_id,
                    postal_code=suggestion.data.postal_code,
                    timezone=suggestion.data.timezone,
                )
            )
        return CitySuggestResponse(items=items, degraded=False)

    async def resolve(self, city: str, region: str | None = None) -> City | None:
        """Найти город по названию — вход расчёта, где ФИАС не приходит.

        Контракт API даёт адрес строкой, а коды перевозчиков привязаны к ФИАС,
        поэтому название нужно во что-то разрешить. Порядок важен:

        1. Локальный справочник. Расчёт вызывается на каждый запрос оператора,
           и ходить за этим к ДаData значило бы платить квотой за каждое
           обращение и зависеть от чужой доступности в горячем пути.
        2. Стандартизация ДаData — только если локально не нашлось. Найденный
           город записывается в справочник, поэтому второй раз за ним уже
           не пойдут.

        Регион работает вето, а не подсказкой: Ростов Ярославской области
        и Ростов-на-Дону — разные города, и выбрать «похожий» из другого
        региона хуже, чем не выбрать никакой.
        """
        for candidate in await self._cities.search(city, limit=10):
            if candidate.name.lower() != city.strip().lower():
                continue
            if region and not self._region_matches(candidate, region):
                continue
            return candidate

        if self._dadata is None or not self._dadata.has_cleaner_credentials:
            return None

        query = f"{region}, {city}" if region else city
        try:
            data = await self._dadata.clean_address(query)
        except DirectoryError as exc:
            log.warning("directories.resolve_degraded", code=exc.code)
            return None
        if data is None:
            return None

        key = resolve_city_key(data)
        if key is None:
            return None
        return await self._ensure_city(key, data.postal_code, data.timezone)

    @staticmethod
    def _region_matches(candidate: City, region: str) -> bool:
        """Совпадает ли регион кандидата с заявленным.

        Сравнение по вхождению: «Ярославская обл» и «Ярославская область» —
        один регион, а точное равенство отбросило бы оба написания.
        """
        # split() по строке без слов даёт пустой список, поэтому берётся
        # первый элемент через next, а не по индексу: регион из одних пробелов
        # или точки — не повод ронять весь расчёт.
        words = region.strip().lower().removesuffix(".").split()
        needle = next(iter(words), "")
        if not needle:
            return True
        haystack = " ".join(filter(None, (candidate.full_name, candidate.region))).lower()
        return needle in haystack

    async def _local(self, query: str, limit: int, *, reason: str) -> CitySuggestResponse:
        """Подсказки из локального справочника — путь деградации."""
        cities = await self._cities.search(query, limit)
        return CitySuggestResponse(
            items=[
                CitySuggestion(
                    fias_id=city.fias_id,
                    parent_fias_id=city.parent_fias_id,
                    fias_level=city.fias_level or 4,
                    name=city.name,
                    full_name=city.full_name or city.name,
                    region=city.region,
                    kladr_id=city.kladr_id,
                    postal_code=city.postal_code,
                    timezone=city.timezone,
                )
                for city in cities
            ],
            degraded=True,
            degraded_reason=reason,
        )

    async def _ensure_city(
        self, key: CityKey, postal_code: str | None, timezone: str | None
    ) -> City:
        """Записать город в справочник по факту обращения.

        В ``full_name`` попадает только то, что собрано из полей городского
        уровня: таблица общая для всех тенантов и под RLS не находится,
        поэтому улица и дом получателя в неё попасть не могут (12.7 ТЗ).
        """
        return await self._cities.upsert(
            {
                "fias_id": key.fias_id,
                "name": key.name,
                "full_name": key.full_name,
                "fias_level": key.fias_level,
                "parent_fias_id": key.parent_fias_id,
                "region_fias_id": key.region_fias_id,
                "kladr_id": key.kladr_id,
                "postal_code": postal_code,
                "timezone": timezone,
            }
        )


class AddressService:
    """Нормализация адреса, введённого строкой."""

    def __init__(self, session: AsyncSession, dadata: DadataClient | None) -> None:
        self._session = session
        self._dadata = dadata
        self._cities = CityService(session, dadata)

    async def normalize(self, query: str) -> NormalizedAddress:
        """Привести строку адреса к ФИАС и оценить пригодность.

        Недоступность ДаData не ошибка: адрес возвращается ненормализованным
        и помеченным, ввести и сохранить его по-прежнему можно.
        """
        if self._dadata is None or not self._dadata.has_cleaner_credentials:
            return NormalizedAddress(fitness=AddressFitness.UNUSABLE.value, degraded=True)

        try:
            data = await self._dadata.clean_address(query)
        except DirectoryError as exc:
            log.warning("directories.normalize_degraded", code=exc.code, query_length=len(query))
            return NormalizedAddress(fitness=AddressFitness.UNUSABLE.value, degraded=True)

        if data is None:
            return NormalizedAddress(fitness=AddressFitness.UNUSABLE.value)

        key = resolve_city_key(data)
        fitness, blockers = assess_fitness(data, key)
        if key is not None:
            await self._cities._ensure_city(key, data.postal_code, data.timezone)

        return NormalizedAddress(
            city_fias_id=key.fias_id if key else None,
            city_parent_fias_id=key.parent_fias_id if key else None,
            city_name=key.name if key else None,
            region=data.region_with_type,
            postal_code=data.postal_code,
            street=data.street_with_type,
            house=data.house,
            flat=data.flat,
            fitness=fitness.value,
            blockers=[b.value for b in blockers],
        )

    async def find_party(self, inn: str, kpp: str | None) -> PartyDraft:
        """Черновик контрагента по ИНН (FR-8.4)."""
        if self._dadata is None:
            raise NotFound("Поиск по ИНН недоступен: справочник не настроен")
        draft = await self._dadata.find_party_by_inn(inn, kpp)
        if draft is None:
            raise NotFound("Организация с таким ИНН не найдена")
        return draft


class CityMappingService:
    """Сопоставление городов ФИАС с кодами перевозчиков (FR-8.2, FR-12.3)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._mappings = CityMappingRepository(session)
        self._cities = CityRepository(session)
        self._carriers = CarrierRepository(session)
        self._terminals = TerminalRepository(session)

    async def resolve(self, carrier_id: UUID, city_fias_id: str) -> str | None:
        """Код города у перевозчика, либо ``None``.

        Отсутствие сопоставления — не исключение. На пути расчёта оно даёт
        отдельную строку выдачи с понятной причиной, и перевозчик не вызывается
        вообще: три секунды таймаута из общего дедлайна в пять экономятся
        (FR-1.3, FR-1.4).
        """
        mapping = await self._mappings.resolve(carrier_id, city_fias_id)
        return mapping.carrier_city_code if mapping else None

    async def resolve_with_fallback(
        self, carrier_id: UUID, city_fias_id: str
    ) -> tuple[str | None, bool]:
        """Код города с управляемым откатом на родителя.

        Для Алупки родитель — Ялта, для Зеленограда — Москва. Откат помечается
        флагом, чтобы вызывающий слой мог сообщить о нём пользователю, а не
        молча отправить груз в соседний город.
        """
        direct = await self.resolve(carrier_id, city_fias_id)
        if direct is not None:
            return direct, False

        city = await self._cities.get_by_fias(city_fias_id)
        if city is None or city.parent_fias_id is None:
            return None, False

        parent_code = await self.resolve(carrier_id, city.parent_fias_id)
        return parent_code, parent_code is not None

    async def match_carrier_cities(
        self, carrier_id: UUID, rows: list[CarrierCity]
    ) -> dict[str, int]:
        """Сопоставить справочник городов перевозчика с ФИАС.

        Возвращает счётчики по методам сопоставления — их видно в отчёте
        синхронизации и в аудите.
        """
        counters: dict[str, int] = {"fias": 0, "kladr": 0, "exact_name": 0, "queued": 0}

        for row in rows:
            result = await self._match_one(row)
            if result.city_fias_id is None:
                await self._mappings.enqueue(
                    carrier_id=carrier_id,
                    carrier_city_code=row.code,
                    carrier_city_name=row.name,
                    carrier_region_name=row.region,
                    reason=result.reason or "no_match",
                    candidates=result.candidates,
                    best_score=result.score or None,
                    terminals_count=row.terminals_count,
                )
                counters["queued"] += 1
                continue

            await self._mappings.upsert(
                carrier_id=carrier_id,
                city_fias_id=result.city_fias_id,
                carrier_city_code=row.code,
                carrier_city_name=row.name,
                match_method=result.method,
                match_score=result.score,
                is_confirmed=result.is_confirmed,
            )
            counters[result.method] = counters.get(result.method, 0) + 1

        return counters

    async def _match_one(self, row: CarrierCity) -> MatchResult:
        """Лестница сопоставления: от идентификатора к имени.

        Порядок от детерминированного к вероятностному. Автоподтверждение даётся
        только идентификаторам и точному имени: высокая оценка похожести — это
        повод показать человеку, а не повод записать без спроса.
        """
        # 1. Перевозчик сам отдал ФИАС — сомнений нет.
        if row.fias_id and await self._cities.get_by_fias(row.fias_id):
            return MatchResult(row.fias_id, "fias", 1.0, True, [])

        # 2. КЛАДР, приведённый к коду населённого пункта.
        kladr = city_kladr_id(row.kladr_id)
        if kladr:
            by_kladr = await self._cities.search(row.name, limit=25)
            hits = [c for c in by_kladr if c.kladr_id == kladr]
            if len(hits) == 1:
                return MatchResult(hits[0].fias_id, "kladr", 0.99, True, [])

        # 3. Имя. Регион здесь работает как вето: одноимённые населённые пункты —
        #    норма для России, и имя без региона пункт назначения не определяет.
        candidates = await self._cities.search(row.name, limit=25)
        scored: list[tuple[float, City]] = []
        for city in candidates:
            score = self._score(row, city)
            if score > 0:
                scored.append((score, city))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        payload: list[dict[str, object]] = [
            {"fias_id": c.fias_id, "name": c.name, "region": c.region, "score": round(s, 3)}
            for s, c in scored[:5]
        ]
        if not scored:
            return MatchResult(None, "none", 0.0, False, payload, reason="no_match")

        best_score, best_city = scored[0]
        if len(scored) > 1 and best_score - scored[1][0] < AMBIGUITY_MARGIN:
            # Два похожих кандидата — ровно та ситуация, ради которой существует
            # очередь ручного сопоставления: человек различит их за секунду.
            return MatchResult(None, "none", best_score, False, payload, reason="ambiguous")

        if best_score < FUZZY_THRESHOLD:
            return MatchResult(None, "none", best_score, False, payload, reason="no_match")

        return MatchResult(
            best_city.fias_id,
            "exact_name" if best_score >= AUTO_CONFIRM_THRESHOLD else "fuzzy_name",
            best_score,
            best_score >= AUTO_CONFIRM_THRESHOLD,
            payload,
        )

    @staticmethod
    def _score(row: CarrierCity, city: City) -> float:
        """Похожесть названий с вето по региону."""
        name_score = SequenceMatcher(None, _norm(row.name), _norm(city.name)).ratio()
        if row.region and city.region:
            if _norm(row.region) not in _norm(city.region) and _norm(city.region) not in _norm(
                row.region
            ):
                # Регион известен и не совпал — кандидат не рассматривается вовсе.
                return 0.0
            return name_score
        # Регион неизвестен: оценка снижается, чтобы такие записи уходили
        # в очередь, а не подтверждались автоматически.
        return name_score * 0.8

    async def confirm(self, item_id: UUID, city_fias_id: str, user_id: UUID | None) -> None:
        """Подтвердить сопоставление вручную (FR-12.3)."""
        item = await self._mappings.get_queue_item(item_id)
        if item is None:
            raise NotFound("Запись очереди не найдена")

        await self._mappings.upsert(
            carrier_id=item.carrier_id,
            city_fias_id=city_fias_id,
            carrier_city_code=item.carrier_city_code,
            carrier_city_name=item.carrier_city_name,
            match_method="manual",
            match_score=1.0,
            is_confirmed=True,
        )
        item.resolved_at = utcnow()
        item.resolved_by_user_id = user_id
        item.resolved_city_fias_id = city_fias_id


def _norm(value: str) -> str:
    """Привести название к сравнимому виду: регистр, ё и дефисы."""
    return value.strip().lower().replace("ё", "е").replace("-", " ")
