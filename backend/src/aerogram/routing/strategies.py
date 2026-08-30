"""Четыре стратегии выбора. Чистые функции над снимком предложений.

Здесь нет ни базы, ни перевозчиков: на вход подаются уже нормализованные
факты о предложениях, на выход — порядок и объяснение. Так стратегию можно
пересчитать на историческом снимке, ради чего ТЗ и требует хранить
``algorithm_version`` (ADR-0014).

Три стратегии тривиальны и не нуждаются в весах: самый дешёвый, самый быстрый,
самый надёжный. Веса нужны только «оптимальному», и они не показываются
оператору (продуктовое ТЗ, раздел 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from aerogram.shared.enums import RiskLevel, RoutingStrategy, ScoreConfidence
from aerogram.shared.money import Money

__all__ = [
    "ALGORITHM_VERSION",
    "OPTIMAL_WEIGHTS",
    "OfferFacts",
    "Ranking",
    "rank",
]

#: Версия формулы. Меняется при ЛЮБОМ изменении весов или правил ранжирования:
#: без этого исторические рекомендации нельзя ни воспроизвести, ни сравнить.
ALGORITHM_VERSION = "routing-1.0.0"


@dataclass(frozen=True, slots=True)
class OptimalWeights:
    """Веса «оптимального» выбора.

    **Значения требуют решения человека** (CLAUDE.md §7, пункт 8). Здесь они
    заданы как рабочее начальное приближение, а не как обоснованная бизнесом
    величина: цена весит больше срока, срок больше надёжности, риск замыкает.
    Пересмотр весов обязан менять ``ALGORITHM_VERSION``.
    """

    cost: Decimal = Decimal("0.40")
    time: Decimal = Decimal("0.30")
    reliability: Decimal = Decimal("0.20")
    risk: Decimal = Decimal("0.10")


OPTIMAL_WEIGHTS = OptimalWeights()

#: Вклад показателя, о котором нет данных. Половина шкалы: перевозчик без
#: истории не хорош и не плох. Одинаковое значение для всех таких перевозчиков
#: означает, что при полном отсутствии данных показатель не влияет на порядок
#: вовсе, и выбор сводится к цене и сроку — как и требует раздел 11 ТЗ.
NEUTRAL = Decimal("0.5")

_RISK_SCALE = {RiskLevel.LOW: Decimal("0"), RiskLevel.MEDIUM: NEUTRAL, RiskLevel.HIGH: Decimal("1")}


@dataclass(frozen=True, slots=True)
class OfferFacts:
    """Факты о предложении, на которых работают стратегии."""

    offer_id: UUID
    carrier_id: UUID
    total: Money
    eta: datetime | None
    eligible: bool
    on_time_probability: Decimal | None = None
    risk: RiskLevel | None = None
    carrier_score: int | None = None
    deadline_margin_seconds: int | None = None
    lateness_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class Ranking:
    """Результат работы стратегии."""

    #: Предложения по возрастанию оценки: первое — рекомендуемое.
    ordered: tuple[OfferFacts, ...]
    #: Уверенность в рекомендации: зависит от полноты данных, а не от разрыва
    #: между вариантами.
    confidence: ScoreConfidence

    @property
    def best(self) -> OfferFacts | None:
        return self.ordered[0] if self.ordered else None


def rank(offers: list[OfferFacts], strategy: RoutingStrategy) -> Ranking:
    """Упорядочить пригодные предложения по стратегии.

    Непригодные не участвуют в выборе, но и не удаляются из выдачи — их
    показывает расчёт. Здесь они просто не ранжируются: рекомендовать
    вариант, нарушающий жёсткое ограничение, нельзя ни при какой оценке.
    """
    eligible = [o for o in offers if o.eligible]
    if not eligible:
        return Ranking(ordered=(), confidence=_confidence(eligible))

    if strategy is RoutingStrategy.CHEAPEST:
        ordered = sorted(eligible, key=lambda o: (o.total.amount_minor, _eta_key(o)))
    elif strategy is RoutingStrategy.FASTEST:
        ordered = sorted(eligible, key=lambda o: (_eta_key(o), o.total.amount_minor))
    elif strategy is RoutingStrategy.RELIABLE:
        # Меньше — лучше, поэтому надёжность инвертируется.
        ordered = sorted(eligible, key=lambda o: (_reliability_cost(o), o.total.amount_minor))
    else:
        scores = _optimal_scores(eligible)
        ordered = sorted(eligible, key=lambda o: (scores[o.offer_id], o.total.amount_minor))

    return Ranking(ordered=tuple(ordered), confidence=_confidence(eligible))


#: Срок, который заведомо позже любого реального. Предложение без известного
#: срока уходит в конец: поставить его в начало значило бы рекомендовать как
#: самый быстрый вариант, о сроке которого ничего не известно.
_NEVER = datetime.max.replace(tzinfo=UTC)


def _eta_key(offer: OfferFacts) -> datetime:
    """Ключ сортировки по сроку."""
    return offer.eta or _NEVER


def _reliability_cost(offer: OfferFacts) -> Decimal:
    """Ненадёжность от 0 (лучше всех) до 1. Нет данных — нейтральная середина."""
    if offer.on_time_probability is not None:
        return Decimal(1) - offer.on_time_probability
    if offer.carrier_score is not None:
        return Decimal(1) - Decimal(offer.carrier_score) / Decimal(100)
    return NEUTRAL


def _optimal_scores(offers: list[OfferFacts]) -> dict[UUID, Decimal]:
    """Комбинированная оценка: меньше — лучше.

    Цена не является жёстким потолком: более дорогой вариант получает лучшую
    оценку, если выигрывает по сроку, надёжности и риску настолько, что это
    перевешивает разницу в цене (продуктовое ТЗ, раздел 7).
    """
    cost = _normalise([Decimal(o.total.amount_minor) for o in offers])
    time = _normalise_time([o.eta for o in offers])
    reliability = [_reliability_cost(o) for o in offers]
    risk = [_RISK_SCALE.get(o.risk, NEUTRAL) if o.risk is not None else NEUTRAL for o in offers]

    w = OPTIMAL_WEIGHTS
    return {
        offer.offer_id: (
            w.cost * cost[i] + w.time * time[i] + w.reliability * reliability[i] + w.risk * risk[i]
        )
        for i, offer in enumerate(offers)
    }


def _normalise_time(etas: list[datetime | None]) -> list[Decimal]:
    """Сроки к [0, 1], где 0 — самый ранний.

    Неизвестный срок НЕ участвует в поиске минимума и максимума, а получает
    худшую оценку. Подставить вместо него условно огромное число значило бы
    растянуть шкалу на века: разница между реальными сроками схлопнулась бы
    в ноль, и вес срока молча исчез бы из формулы.
    """
    known = [Decimal(int(eta.timestamp())) for eta in etas if eta is not None]
    if not known:
        return [Decimal(1)] * len(etas)
    low, high = min(known), max(known)
    span = high - low
    return [
        Decimal(1)
        if eta is None
        else (Decimal(0) if span == 0 else (Decimal(int(eta.timestamp())) - low) / span)
        for eta in etas
    ]


def _normalise(values: list[Decimal]) -> list[Decimal]:
    """Привести к [0, 1], где 0 — лучшее (наименьшее) значение.

    Одинаковые значения дают нули: показатель, по которому варианты не
    различаются, не должен влиять на порядок.
    """
    low, high = min(values), max(values)
    if high == low:
        return [Decimal(0)] * len(values)
    span = high - low
    return [(value - low) / span for value in values]


def _confidence(offers: list[OfferFacts]) -> ScoreConfidence:
    """Уверенность по полноте данных, а не по разрыву между вариантами.

    Система не должна изображать точность, которой у неё нет (системное ТЗ,
    раздел 9): пока фактических доставок мало, у предложений нет ни
    вероятности, ни риска, и уверенность честно низкая.
    """
    if not offers:
        return ScoreConfidence.LOW
    with_data = sum(1 for o in offers if o.on_time_probability is not None)
    if with_data == len(offers):
        return ScoreConfidence.HIGH
    if with_data:
        return ScoreConfidence.MEDIUM
    return ScoreConfidence.LOW
