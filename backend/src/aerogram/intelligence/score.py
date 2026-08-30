"""Формула Carrier Score. Чистая арифметика, без базы и без сети.

Методика задана ТЗ, раздел 10, и реализуется здесь буквально. Отдельный модуль
без зависимостей нужен затем, чтобы формулу можно было прочитать целиком
за один присест: это тот код, где ошибка не падает, а тихо меняет советы,
которые продукт даёт клиенту.

Главная опасность функции — **холодный старт** (раздел 10.2). На старте данных
нет, а показать выдуманную цифру хуже, чем не показать никакой: один неверный
совет в первый месяц дороже, чем отсутствие функции. Отсюда три правила,
которые здесь и живут:

* компоненты сглаживаются к среднему по платформе, поэтому три наблюдения
  не дают ни ста баллов, ни нуля;
* при выборке меньше десяти скор **не считается вовсе** — наружу уходит
  «недостаточно данных», а не число;
* уверенность выдаётся вместе со скором и никогда не подразумевается.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from aerogram.shared.enums import ScoreConfidence

__all__ = [
    "FORMULA_VERSION",
    "PRIOR_WEIGHT",
    "WEIGHTS",
    "Components",
    "PlatformPrior",
    "confidence_for",
    "score_from",
    "smooth",
]

#: Версия формулы. Меняется вместе с весами или составом компонентов —
#: без этого исторические снапшоты станут несопоставимыми, а FR-7.4 требует
#: ровно обратного: изменение весов не переписывает историю.
FORMULA_VERSION = "score-1.0.0"

#: Веса компонентов (раздел 10.1). Сумма — единица; проверяется тестом,
#: потому что «почти единица» тихо сместила бы шкалу у всех перевозчиков.
WEIGHTS: dict[str, Decimal] = {
    "on_time": Decimal("0.35"),
    "reliability": Decimal("0.20"),
    "incident_free": Decimal("0.20"),
    "price_index": Decimal("0.15"),
    "data_quality": Decimal("0.10"),
}

#: Приорный вес байесовского сглаживания (раздел 10.2). Двадцать наблюдений
#: «доверия авансом»: при выборке в 5 отправлений собственные данные весят
#: одну пятую, при 100 — уже пять шестых.
PRIOR_WEIGHT = 20

#: Пороги доверия (FR-7.3). Ниже нижнего скор не показывается вовсе.
_CONFIDENCE_THRESHOLDS: tuple[tuple[int, ScoreConfidence], ...] = (
    (100, ScoreConfidence.HIGH),
    (30, ScoreConfidence.MEDIUM),
    (10, ScoreConfidence.LOW),
)

#: Минимальная выборка, при которой скор вообще считается.
MIN_SAMPLE = 10


@dataclass(frozen=True, slots=True)
class Components:
    """Наблюдаемые доли по выборке. Каждая в [0; 1].

    ``None`` означает «не наблюдалось», а не ноль: перевозчик без единого
    инцидента и перевозчик, по которому инциденты не считались, — разные
    вещи, и подставить ноль значило бы наградить второго.
    """

    on_time: Decimal | None = None
    reliability: Decimal | None = None
    incident_free: Decimal | None = None
    price_index: Decimal | None = None
    data_quality: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PlatformPrior:
    """Средние по платформе — то, к чему притягивается малая выборка.

    Значения по умолчанию нейтральные, а не оптимистичные: пока платформа
    сама ничего не измерила, приор не должен выдавать аванс доверия.
    """

    on_time: Decimal = Decimal("0.5")
    reliability: Decimal = Decimal("0.5")
    incident_free: Decimal = Decimal("0.5")
    price_index: Decimal = Decimal("0.5")
    data_quality: Decimal = Decimal("0.5")


def smooth(observed: Decimal | None, prior: Decimal, sample_size: int) -> Decimal:
    """Байесовское сглаживание: ``(x·n + p·m) / (n + m)``, где ``m`` — приор.

    Не наблюдалось — остаётся приор целиком: подставлять ноль значило бы
    выдать отсутствие данных за плохой результат.
    """
    if observed is None or sample_size <= 0:
        return prior
    n = Decimal(sample_size)
    m = Decimal(PRIOR_WEIGHT)
    return (observed * n + prior * m) / (n + m)


def confidence_for(sample_size: int) -> ScoreConfidence:
    """Статус доверия по размеру выборки (FR-7.3)."""
    for threshold, level in _CONFIDENCE_THRESHOLDS:
        if sample_size >= threshold:
            return level
    return ScoreConfidence.INSUFFICIENT


def score_from(
    components: Components, sample_size: int, prior: PlatformPrior | None = None
) -> tuple[int | None, ScoreConfidence]:
    """Скор 0–100 и статус доверия.

    ``None`` вместо числа — это не ошибка и не ноль, а «недостаточно данных»:
    интерфейс обязан показать именно эти слова (FR-7.3), потому что ноль
    читается как «худший перевозчик», а он всего лишь новый.
    """
    confidence = confidence_for(sample_size)
    if confidence is ScoreConfidence.INSUFFICIENT:
        return None, confidence

    base = prior or PlatformPrior()
    smoothed = {
        "on_time": smooth(components.on_time, base.on_time, sample_size),
        "reliability": smooth(components.reliability, base.reliability, sample_size),
        "incident_free": smooth(components.incident_free, base.incident_free, sample_size),
        "price_index": smooth(components.price_index, base.price_index, sample_size),
        "data_quality": smooth(components.data_quality, base.data_quality, sample_size),
    }
    total = sum(WEIGHTS[name] * value for name, value in smoothed.items())
    points = (Decimal(100) * total).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    # Обрезка — страховка от компонента вне [0; 1], пришедшего из агрегата:
    # шкала обещана как 0–100, и ограничение таблицы это же и требует.
    return int(min(max(points, Decimal(0)), Decimal(100))), confidence
