"""Carrier Intelligence: пересчёт скора и выдача аналитики.

Модуль работает на чтение домена и пишет только собственные снапшоты
(CLAUDE.md §4, пункт 4). К перевозчикам он не обращается вовсе.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.directories.repository import CarrierRepository
from aerogram.intelligence.models import CarrierScoreSnapshot
from aerogram.intelligence.repository import Observations, ScoreRepository
from aerogram.intelligence.schemas import CarrierAnalyticsOut, ScoreComponentsOut
from aerogram.intelligence.score import (
    FORMULA_VERSION,
    Components,
    PlatformPrior,
    score_from,
)
from aerogram.shared.enums import ScoreConfidence, ScoreScope
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

    async def recalculate(self, period_start: date, period_end: date) -> list[CarrierScoreSnapshot]:
        """Пересчитать скор за период и сохранить снапшоты.

        Пересчёт одного и того же периода той же версией формулы заменяет
        прошлый снапшот, а не плодит второй: два разных ответа об одном
        периоде нельзя ни объяснить, ни использовать.
        """
        observations = await self._scores.observations(period_start, period_end)
        prior = _prior_from(observations)
        medians = [o.median_cost_minor for o in observations if o.median_cost_minor is not None]
        market_median = sorted(medians)[len(medians) // 2] if medians else None

        snapshots: list[CarrierScoreSnapshot] = []
        for observed in observations:
            components = _components(observed, market_median)
            score, confidence = score_from(components, observed.finalized, prior)
            snapshots.append(
                await self._scores.upsert(
                    CarrierScoreSnapshot(
                        id=uuid7(),
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
                        score=score,
                        confidence=confidence,
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


def _prior_from(observations: list[Observations]) -> PlatformPrior:
    """Среднее по платформе — то, к чему притягивается малая выборка.

    Считается по всем перевозчикам сразу и по суммарным счётчикам, а не как
    среднее долей: иначе перевозчик с тремя отправлениями влиял бы на приор
    так же, как перевозчик с тремя тысячами.
    """
    if not observations:
        return PlatformPrior()

    finalized = sum(o.finalized for o in observations)
    with_deadline = sum(o.with_deadline for o in observations)
    defaults = PlatformPrior()
    return PlatformPrior(
        on_time=_rate(sum(o.on_time for o in observations), with_deadline) or defaults.on_time,
        reliability=(
            Decimal(1) - (_rate(sum(o.broken for o in observations), finalized) or Decimal(0))
            if finalized
            else defaults.reliability
        ),
        incident_free=(
            Decimal(1)
            - (_rate(sum(o.with_incident for o in observations), finalized) or Decimal(0))
            if finalized
            else defaults.incident_free
        ),
        # Медиана по определению делит выборку пополам, поэтому приор цены —
        # ровно середина шкалы, а не среднее индексов.
        price_index=defaults.price_index,
        data_quality=(
            _rate(sum(o.transparent for o in observations), finalized) or defaults.data_quality
        ),
    )
