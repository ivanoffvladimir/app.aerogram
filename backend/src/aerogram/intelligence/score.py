"""Формула Carrier Score. Чистая арифметика, без базы и без сети.

Методика задана ТЗ, раздел 10, и реализуется здесь буквально. Отдельный модуль
без зависимостей нужен затем, чтобы формулу можно было прочитать целиком
за один присест: это тот код, где ошибка не падает, а тихо меняет советы,
которые продукт даёт клиенту.

Главная опасность функции — **холодный старт** (раздел 10.2), и решается он
здесь двумя разными вещами, которые легко перепутать.

**Первое: у перевозчика есть оценка до первого отправления клиента.**
Она берётся из платформенной базы — свода по завершённым отправлениям всех
клиентов, посчитанного **по каждому перевозчику отдельно** (ADR-0026). Иначе
рейтинга не существует: одинаковый на всех приор дал бы всем перевозчикам
один и тот же скор, и выбирать было бы не из чего.

**Второе: собственный опыт клиента вытесняет платформенный постепенно.**
Наблюдения тенанта притягиваются к базе байесовским сглаживанием с весом
``PRIOR_WEIGHT``: при пяти своих отправлениях они весят одну пятую, при ста —
пять шестых. Резкого переключения нет нигде, поэтому скор не прыгает в день,
когда набралась выборка.

Отсюда три правила, которые здесь и живут:

* компоненты сглаживаются к платформенной базе, поэтому три наблюдения
  не дают ни ста баллов, ни нуля;
* скор **не считается вовсе**, когда нет ни своей выборки от ``MIN_SAMPLE``,
  ни платформенной базы: наружу уходит «недостаточно данных», а не число;
* вместе со скором всегда выдаются доверие и **основание** — на чьих данных
  он стоит. Число без этого клиент не обязан принимать на веру.

**Денег в платформенной базе нет.** Индекс цены считается только по своим
отправлениям и к платформенной базе не притягивается: тариф — коммерческая
тайна клиента, и усреднённый по платформе он рассказал бы соседям об уровне
чужих договоров. Приор цены остаётся серединой шкалы, и это не заглушка:
медиана по определению делит выборку пополам.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from aerogram.shared.enums import ScoreBasis, ScoreConfidence

__all__ = [
    "FORMULA_VERSION",
    "MIN_PLATFORM_SAMPLE",
    "MIN_PLATFORM_TENANTS",
    "MIN_SAMPLE",
    "PRIOR_WEIGHT",
    "WEIGHTS",
    "Components",
    "PlatformPrior",
    "ScoreResult",
    "basis_for",
    "confidence_for",
    "score_from",
    "smooth",
]

#: Версия формулы. Меняется вместе с весами, составом компонентов или
#: правилом, по которому берётся приор, — без этого исторические снапшоты
#: станут несопоставимыми, а FR-7.4 требует ровно обратного: изменение
#: методики не переписывает историю.
#:
#: 2.0.0 — приор перестал быть общим на всех перевозчиков и стал
#: платформенной базой по каждому (ADR-0026). Числа этой версии нельзя
#: сравнивать с числами 1.0.0: там при малой выборке все перевозчики
#: сходились к одному значению.
FORMULA_VERSION = "score-2.0.0"

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

#: Пороги доверия (FR-7.3) по СОБСТВЕННОЙ выборке тенанта.
_CONFIDENCE_THRESHOLDS: tuple[tuple[int, ScoreConfidence], ...] = (
    (100, ScoreConfidence.HIGH),
    (30, ScoreConfidence.MEDIUM),
    (10, ScoreConfidence.LOW),
)

#: Минимальная собственная выборка, при которой скор считается без опоры
#: на платформенную базу.
MIN_SAMPLE = 10

#: Сколько разных клиентов должны возить перевозчиком, чтобы свод по нему
#: перестал указывать на конкретного из них. Решение человека от 5 сентября
#: 2026 (ADR-0026): при двух каждый вычитает себя из агрегата и получает
#: статистику второго почти точно.
MIN_PLATFORM_TENANTS = 3

#: Минимальная выборка платформенной базы. Тридцать — тот же порог, с которого
#: начинается «среднее» доверие: число, которое показывается КАЖДОМУ клиенту,
#: не должно стоять на десяти наблюдениях.
MIN_PLATFORM_SAMPLE = 30


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
    """База, к которой притягивается малая выборка тенанта.

    Своя у каждого перевозчика: общая на всех не давала бы рейтинга вовсе —
    при нулевой собственной выборке все перевозчики получили бы одинаковый
    скор, и сравнивать было бы нечего.

    Значения по умолчанию нейтральные, а не оптимистичные: пока платформа
    сама ничего не измерила, база не должна выдавать аванс доверия.

    ``price_index`` намеренно остаётся серединой шкалы и в платформенный
    свод не входит: тариф — коммерческая тайна клиента.
    """

    on_time: Decimal = Decimal("0.5")
    reliability: Decimal = Decimal("0.5")
    incident_free: Decimal = Decimal("0.5")
    price_index: Decimal = Decimal("0.5")
    data_quality: Decimal = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Скор и то, чем он подкреплён.

    Три значения вместе, а не число отдельно: скор без доверия и основания
    интерфейс показать не имеет права (FR-7.3, FR-7.5), и разнести их
    по разным возвращаемым значениям значит однажды показать одно без других.
    """

    score: int | None
    confidence: ScoreConfidence
    basis: ScoreBasis


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


def confidence_for(sample_size: int, *, has_baseline: bool = False) -> ScoreConfidence:
    """Статус доверия (FR-7.3).

    Считается по СОБСТВЕННОЙ выборке тенанта: доверие отвечает на вопрос
    «насколько это про нас», и платформенная база его не повышает — она
    считается по чужим договорам и чужим направлениям.

    Ниже нижнего порога доверие всё же не ``insufficient``, если есть
    платформенная база: данные существуют, их просто собрали не мы,
    и это «низкое доверие», а не «данных нет». Разницу видно в основании
    (``ScoreBasis``), которое выдаётся рядом.
    """
    for threshold, level in _CONFIDENCE_THRESHOLDS:
        if sample_size >= threshold:
            return level
    return ScoreConfidence.LOW if has_baseline else ScoreConfidence.INSUFFICIENT


def basis_for(sample_size: int, *, has_baseline: bool = False) -> ScoreBasis:
    """На чьих данных стоит число.

    Собственная выборка ниже ``MIN_SAMPLE`` не делает основание смешанным:
    три отправления при весе приора в двадцать сдвигают результат на седьмую
    часть, и называть это «своей оценкой» значило бы приписать клиенту вывод,
    которого его данные не поддерживают.
    """
    if not has_baseline:
        return ScoreBasis.OWN if sample_size >= MIN_SAMPLE else ScoreBasis.NONE
    return ScoreBasis.MIXED if sample_size >= MIN_SAMPLE else ScoreBasis.PLATFORM


def score_from(
    components: Components, sample_size: int, prior: PlatformPrior | None = None
) -> ScoreResult:
    """Скор 0–100, доверие и основание.

    ``prior`` — платформенная база этого перевозчика. ``None`` означает,
    что свода по нему нет: слишком мало клиентов или слишком мало отправлений,
    — и тогда скор считается только по собственной выборке, а при её нехватке
    не считается вовсе.

    ``score is None`` — это не ошибка и не ноль, а «недостаточно данных»:
    интерфейс обязан показать именно эти слова (FR-7.3), потому что ноль
    читается как «худший перевозчик», а он всего лишь новый.
    """
    has_baseline = prior is not None
    confidence = confidence_for(sample_size, has_baseline=has_baseline)
    basis = basis_for(sample_size, has_baseline=has_baseline)
    if basis is ScoreBasis.NONE:
        return ScoreResult(None, ScoreConfidence.INSUFFICIENT, basis)

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
    return ScoreResult(int(min(max(points, Decimal(0)), Decimal(100))), confidence, basis)
