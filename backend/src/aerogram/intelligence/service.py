"""Carrier Intelligence: пересчёт скора и выдача аналитики.

Модуль работает на чтение домена и пишет только собственные снапшоты
(CLAUDE.md §4, пункт 4). К перевозчикам он не обращается вовсе.

Пересчёт двухшаговый (ADR-0026), и порядок шагов важен.

**Сначала свод по платформе.** Фоновая задача обходит тенантов, складывает
их счётчики по каждому перевозчику и записывает базу — но только там, где
свод обезличен: не меньше трёх клиентов и тридцати отправлений. Обход
тенантов новых прав не требует (ADR-0015).

**Потом скор каждого тенанта.** Его наблюдения притягиваются к базе своего
перевозчика. Клиент, который этим перевозчиком ещё не возил, видит оценку
платформы; по мере накопления собственных отправлений число плавно
смещается к его собственному опыту.

Обратный порядок дал бы клиентам скор на вчерашней базе — не ошибка, но
лишние сутки задержки на каждое изменение.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.directories.repository import CarrierRepository
from aerogram.intelligence.models import CarrierPlatformBaseline, CarrierScoreSnapshot
from aerogram.intelligence.platform import PlatformTotals, accumulate, prior_from_totals
from aerogram.intelligence.repository import Observations, ScoreRepository
from aerogram.intelligence.schemas import CarrierAnalyticsOut, ScoreComponentsOut
from aerogram.intelligence.score import (
    FORMULA_VERSION,
    Components,
    PlatformPrior,
    score_from,
)
from aerogram.shared.enums import ScoreBasis, ScoreConfidence, ScoreScope
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger

__all__ = ["SCOPE_CASCADE", "ScoreService"]

log = get_logger(__name__)

#: Каскад разрезов от узкого к широкому (FR-7.2). Значение берётся из первого,
#: где выборки хватило: узкий разрез точнее, широкий — надёжнее.
SCOPE_CASCADE: tuple[ScoreScope, ...] = (
    ScoreScope.DIRECTION_WEIGHT,
    ScoreScope.DIRECTION,
    ScoreScope.GLOBAL,
)


def _rate(part: int, whole: int) -> Decimal | None:
    """Доля или ``None``, если делить не на что.

    Ноль наблюдений — это «не наблюдалось», а не «ноль процентов»: разница
    решает, накажет формула перевозчика или оставит его на приоре.
    """
    if whole <= 0:
        return None
    return (Decimal(part) / Decimal(whole)).quantize(Decimal("0.0001"))


def _price_index(median_cost: int | None, market_median: int | None) -> Decimal | None:
    """Положение цены относительно медианы выборки, обрезанное в [0; 1].

    Ровно по медиане — половина шкалы, вдвое дешевле — единица, вдвое дороже —
    ноль. Центр в 0.5, а не в 1, намеренно: иначе все, кто дешевле медианы,
    получали бы одинаковый максимум и перестали бы различаться.

    Точный вид преобразования ТЗ не задаёт — раздел 10.1 требует лишь
    «нормированное положение относительно медианы». Выбор вынесен в
    docs/status.md на подтверждение вместе с весами.
    """
    if median_cost is None or not market_median:
        return None
    shift = Decimal(market_median - median_cost) / (Decimal(2) * Decimal(market_median))
    return min(max(Decimal("0.5") + shift, Decimal(0)), Decimal(1)).quantize(Decimal("0.0001"))


class ScoreService:
    """Скор перевозчиков."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._scores = ScoreRepository(session)
        self._carriers = CarrierRepository(session)

    async def recalculate(
        self, period_start: date, period_end: date, *, tenant_id: UUID
    ) -> list[CarrierScoreSnapshot]:
        """Пересчитать скор за период и сохранить снапшоты.

        Пересчёт одного и того же периода той же версией формулы заменяет
        прошлый снапшот, а не плодит второй: два разных ответа об одном
        периоде нельзя ни объяснить, ни использовать.

        ``tenant_id`` передаётся явно, а не берётся из настройки сессии:
        снапшот принадлежит тенанту (ADR-0017), и его владелец должен быть
        виден в коде, который его создаёт. RLS проверит то же самое ещё раз —
        запись с чужим тенантом не пройдёт ``WITH CHECK``.
        """
        observations = await self._scores.observations(period_start, period_end)
        baselines = await self._scores.latest_baselines()
        medians = [o.median_cost_minor for o in observations if o.median_cost_minor is not None]
        market_median = sorted(medians)[len(medians) // 2] if medians else None

        # Перевозчик, по которому у тенанта нет ни одного завершённого
        # отправления, наблюдений не даёт вовсе — а оценка платформы у него
        # есть. Пропустить его значило бы оставить клиента без числа ровно
        # там, где база и нужна: до первой отправки.
        seen = {o.carrier_id for o in observations}
        rows: list[Observations] = [
            *observations,
            *(_empty(carrier_id) for carrier_id in baselines if carrier_id not in seen),
        ]

        snapshots: list[CarrierScoreSnapshot] = []
        for observed in rows:
            components = _components(observed, market_median)
            baseline = baselines.get(observed.carrier_id)
            result = score_from(components, observed.finalized, _prior_of(baseline))
            snapshots.append(
                await self._scores.upsert(
                    CarrierScoreSnapshot(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        carrier_id=observed.carrier_id,
                        scope_type=ScoreScope.GLOBAL,
                        scope_key="",
                        period_start=period_start,
                        period_end=period_end,
                        sample_size=observed.finalized,
                        on_time_rate=components.on_time,
                        reliability=components.reliability,
                        incident_rate=_rate(observed.with_incident, observed.finalized),
                        price_index=components.price_index,
                        data_quality=components.data_quality,
                        score=result.score,
                        confidence=result.confidence,
                        basis=result.basis,
                        platform_sample_size=(
                            baseline.sample_size if baseline is not None else None
                        ),
                        formula_version=FORMULA_VERSION,
                    )
                )
            )
        await self._session.flush()
        log.info(
            "intelligence.recalculated",
            carriers=len(snapshots),
            scored=len([s for s in snapshots if s.score is not None]),
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )
        return snapshots

    async def observations(self, period_start: date, period_end: date) -> list[Observations]:
        """Наблюдения тенанта за период — сырьё для платформенного свода.

        Отдельный метод, а не прямой вызов репозитория из воркера: SQL живёт
        в репозитории, а модуль наружу отдаёт свои DTO (CLAUDE.md §4, п. 6).
        """
        return await self._scores.observations(period_start, period_end)

    async def save_baselines(
        self, period_start: date, period_end: date, per_tenant: list[list[Observations]]
    ) -> list[CarrierPlatformBaseline]:
        """Сложить наблюдения тенантов и записать обезличенный свод.

        Записывается **только то, что прошло порог**: свод по одному-двум
        клиентам не сохраняется вовсе, а не прячется при показе. Строки,
        указывающей на конкретного клиента, не должно существовать даже
        в таблице — то же самое требуют ограничения самой таблицы, и это
        не дублирование, а два независимых рубежа (ADR-0026).
        """
        saved: list[CarrierPlatformBaseline] = []
        skipped = 0
        for totals in accumulate(per_tenant):
            if not totals.is_publishable:
                skipped += 1
                continue
            saved.append(
                await self._scores.upsert_baseline(_baseline(totals, period_start, period_end))
            )
        await self._session.flush()
        log.info(
            "intelligence.platform_baselines",
            saved=len(saved),
            skipped_below_threshold=skipped,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )
        return saved

    async def analytics(self) -> list[CarrierAnalyticsOut]:
        """Скор по всем подключённым перевозчикам.

        Перевозчик без снапшота не пропускается: его отсутствие в списке
        оператор прочитал бы как «не подключён», а он подключён и просто
        ещё не набрал статистики.
        """
        rows: list[CarrierAnalyticsOut] = []
        for carrier in await self._carriers.list_active():
            snapshot = await self._best(carrier.id)
            if snapshot is None:
                rows.append(
                    CarrierAnalyticsOut(
                        carrier_id=carrier.id,
                        carrier_code=carrier.code,
                        carrier_name=carrier.name,
                        score=None,
                        confidence=ScoreConfidence.INSUFFICIENT,
                        basis=ScoreBasis.NONE,
                    )
                )
                continue
            rows.append(
                CarrierAnalyticsOut(
                    carrier_id=carrier.id,
                    carrier_code=carrier.code,
                    carrier_name=carrier.name,
                    score=snapshot.score,
                    confidence=snapshot.confidence,
                    basis=ScoreBasis(snapshot.basis),
                    platform_sample_size=snapshot.platform_sample_size,
                    scope_type=ScoreScope(snapshot.scope_type),
                    scope_key=snapshot.scope_key,
                    sample_size=snapshot.sample_size,
                    period_start=snapshot.period_start,
                    period_end=snapshot.period_end,
                    components=ScoreComponentsOut(
                        on_time_rate=snapshot.on_time_rate,
                        reliability=snapshot.reliability,
                        incident_rate=snapshot.incident_rate,
                        price_index=snapshot.price_index,
                        data_quality=snapshot.data_quality,
                    ),
                    formula_version=snapshot.formula_version,
                    calculated_at=snapshot.calculated_at,
                )
            )
        return rows

    async def _best(self, carrier_id: UUID, scope_key: str = "") -> CarrierScoreSnapshot | None:
        """Первый разрез каскада, где скор посчитан (FR-7.2).

        Снапшот с ``insufficient`` пропускается: узкий разрез без данных
        не должен закрывать собой широкий, где данные есть.
        """
        for scope in SCOPE_CASCADE:
            key = "" if scope is ScoreScope.GLOBAL else scope_key
            snapshot = await self._scores.latest(carrier_id, scope, key)
            if snapshot is not None and snapshot.score is not None:
                return snapshot
        return await self._scores.latest(carrier_id, ScoreScope.GLOBAL, "")


def _components(observed: Observations, market_median: int | None) -> Components:
    """Счётчики → доли для формулы."""
    return Components(
        # Доля считается от отправлений СО СРОКОМ: у остальных «вовремя»
        # не определено, и включать их в знаменатель значило бы наказывать
        # перевозчика за то, что клиент не поставил дедлайн.
        on_time=_rate(observed.on_time, observed.with_deadline),
        reliability=(
            None
            if observed.finalized <= 0
            else Decimal(1) - (_rate(observed.broken, observed.finalized) or Decimal(0))
        ),
        incident_free=(
            None
            if observed.finalized <= 0
            else Decimal(1) - (_rate(observed.with_incident, observed.finalized) or Decimal(0))
        ),
        price_index=_price_index(observed.median_cost_minor, market_median),
        data_quality=_rate(observed.transparent, observed.finalized),
    )


def _prior_of(baseline: CarrierPlatformBaseline | None) -> PlatformPrior | None:
    """Платформенная база перевозчика → приор формулы.

    ``None`` означает, что свода по этому перевозчику нет: слишком мало
    клиентов или отправлений. Тогда скор считается только по собственной
    выборке, а при её нехватке не считается вовсе.

    Прежняя версия брала приор из наблюдений САМОГО ТЕНАНТА, усреднённых
    по всем перевозчикам. Это было двумя ошибками сразу: платформенным
    он не был (запрос идёт под RLS), а общий на всех перевозчиков приор
    при малой выборке сводил их к одному числу — рейтинга не получалось.
    """
    if baseline is None:
        return None
    defaults = PlatformPrior()
    return PlatformPrior(
        on_time=baseline.on_time_rate if baseline.on_time_rate is not None else defaults.on_time,
        reliability=(
            baseline.reliability if baseline.reliability is not None else defaults.reliability
        ),
        incident_free=(
            baseline.incident_free if baseline.incident_free is not None else defaults.incident_free
        ),
        # Денег в своде нет: индекс цены считается только внутри тенанта.
        price_index=defaults.price_index,
        data_quality=(
            baseline.data_quality if baseline.data_quality is not None else defaults.data_quality
        ),
    )


def _baseline(
    totals: PlatformTotals, period_start: date, period_end: date
) -> CarrierPlatformBaseline:
    """Свод → строка платформенной базы."""
    prior = prior_from_totals(totals)
    return CarrierPlatformBaseline(
        id=uuid7(),
        carrier_id=totals.carrier_id,
        period_start=period_start,
        period_end=period_end,
        tenants_count=totals.tenants,
        sample_size=totals.finalized,
        on_time_rate=prior.on_time,
        reliability=prior.reliability,
        incident_free=prior.incident_free,
        data_quality=prior.data_quality,
        formula_version=FORMULA_VERSION,
    )


def _empty(carrier_id: UUID) -> Observations:
    """Пустые наблюдения: перевозчик подключён, но клиент им ещё не возил."""
    return Observations(
        carrier_id=carrier_id,
        finalized=0,
        with_deadline=0,
        on_time=0,
        broken=0,
        with_incident=0,
        transparent=0,
        median_cost_minor=None,
    )
