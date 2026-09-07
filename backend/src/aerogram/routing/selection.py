"""Автовыбор: какое предложение берёт правило маршрутизации.

Чистые функции над снимком предложений — как ``strategies.py``, и по той же
причине: выбор без человека обязан быть воспроизводимым на историческом
снимке, иначе через год нечем объяснить, почему взяли именно это.

``SelectionRule`` и ``RoutingStrategy`` — **разные словари**, и это не
недосмотр. Стратегию выбирает оператор вкладкой, правило пишет владелец;
``best_score``, ``best_value`` и ``cheapest_meeting_deadline`` стратегиями
не выражаются, ``reliable`` правилом не выражается. Расхождение выбора
с рекомендацией поэтому нормальный исход, а не ошибка (ADR-0029).

**Воздержание — законный результат.** Правило может совпасть с запросом,
а выбрать оказывается не из чего: ни у кого не известен срок, ни у кого нет
скора, у кандидатов разные валюты. Тогда решения нет и названа причина.
Альтернатива — «выбрать хоть что-то» — и есть скрытая автоматизация, против
которой написан раздел 6 фронт-ТЗ.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from aerogram.routing.strategies import OfferFacts
from aerogram.shared.enums import SelectionRule

__all__ = [
    "SELECTION_VERSION",
    "Abstention",
    "Selection",
    "select",
]

#: Версия формул выбора. Меняется при ЛЮБОЙ правке правил ниже: без этого
#: исторические автоматические решения нельзя ни воспроизвести, ни сравнить
#: с нынешними (CLAUDE.md §7, пункт 8).
SELECTION_VERSION = "selection-1.0.0"

#: Срок, который заведомо позже любого реального — тот же приём, что
#: в ``strategies.py``. Здесь он нужен только как ключ сортировки среди
#: предложений с ИЗВЕСТНЫМ сроком: неизвестный срок в ``fastest`` отсеивается
#: раньше, а не уводится в конец.
_NEVER = datetime.max.replace(tzinfo=UTC)


class Abstention(StrEnum):
    """Почему правило совпало, а решения нет.

    Каждая причина названа отдельно намеренно: «автовыбор не сработал» без
    причины — это ровно тот молчаливый ноль, который эта задача и чинит.
    """

    NO_ELIGIBLE_OFFERS = "no_eligible_offers"
    MIXED_CURRENCIES = "mixed_currencies"
    NO_KNOWN_ETA = "no_known_eta"
    NO_CARRIER_SCORES = "no_carrier_scores"
    NO_ON_TIME_PROBABILITY = "no_on_time_probability"
    NO_DEADLINE_IN_REQUEST = "no_deadline_in_request"
    NO_PROVEN_DEADLINE_MARGIN = "no_proven_deadline_margin"


@dataclass(frozen=True, slots=True)
class Selection:
    """Что выбрало правило. Ровно одно из двух полей заполнено."""

    offer: OfferFacts | None = None
    abstention: Abstention | None = None


def select(offers: Sequence[OfferFacts], rule: SelectionRule, *, deadline_set: bool) -> Selection:
    """Выбрать предложение по правилу автовыбора.

    Кандидаты — только пригодные: запрещённое политикой или не уложившееся
    в срок предложение выбрать нельзя ни при какой оценке (ADR-0028).
    """
    eligible = [offer for offer in offers if offer.eligible]
    if not eligible:
        return Selection(abstention=Abstention.NO_ELIGIBLE_OFFERS)

    # Разные валюты среди кандидатов останавливают выбор целиком. Сравнить
    # суммы разных валют нельзя (CLAUDE.md §6), а на автоматическом пути
    # некому заметить, что «дешевле» оказалось дешевле в тенге.
    if len({offer.total.currency for offer in eligible}) > 1:
        return Selection(abstention=Abstention.MIXED_CURRENCIES)

    if rule is SelectionRule.CHEAPEST:
        return Selection(offer=min(eligible, key=_by_price))

    if rule is SelectionRule.FASTEST:
        known = [offer for offer in eligible if offer.eta is not None]
        if not known:
            return Selection(abstention=Abstention.NO_KNOWN_ETA)
        return Selection(offer=min(known, key=_by_speed))

    if rule is SelectionRule.BEST_SCORE:
        scored = [offer for offer in eligible if offer.carrier_score is not None]
        if not scored:
            return Selection(abstention=Abstention.NO_CARRIER_SCORES)
        return Selection(offer=min(scored, key=_by_score))

    if rule is SelectionRule.BEST_VALUE:
        probable = [
            offer
            for offer in eligible
            if offer.on_time_probability is not None and offer.on_time_probability > 0
        ]
        if not probable:
            return Selection(abstention=Abstention.NO_ON_TIME_PROBABILITY)
        return Selection(offer=min(probable, key=_by_value))

    # cheapest_meeting_deadline
    if not deadline_set:
        # Без дедлайна правило «самый дешёвый в срок» не значит ничего.
        # Подменить его на «самый дешёвый» значило бы исполнить не то
        # правило, которое написал человек.
        return Selection(abstention=Abstention.NO_DEADLINE_IN_REQUEST)
    in_time = [
        offer
        for offer in eligible
        if offer.deadline_margin_seconds is not None and offer.deadline_margin_seconds >= 0
    ]
    if not in_time:
        return Selection(abstention=Abstention.NO_PROVEN_DEADLINE_MARGIN)
    return Selection(offer=min(in_time, key=_by_price))


# Ключи сортировки заканчиваются ``offer_id`` ВЕЗДЕ. Без него ничья
# разрешается порядком строк из базы, и неизменяемое решение перестаёт быть
# воспроизводимым: тот же снимок через год назвал бы другое предложение.


def _by_price(offer: OfferFacts) -> tuple[int, datetime, str]:
    return (offer.total.amount_minor, offer.eta or _NEVER, str(offer.offer_id))


def _by_speed(offer: OfferFacts) -> tuple[datetime, int, str]:
    return (offer.eta or _NEVER, offer.total.amount_minor, str(offer.offer_id))


def _by_score(offer: OfferFacts) -> tuple[int, int, str]:
    # Скор больше — лучше, поэтому знак инвертируется: сравнение везде
    # «меньше значит лучше», и разнобой здесь стоил бы перевёрнутого выбора.
    return (-(offer.carrier_score or 0), offer.total.amount_minor, str(offer.offer_id))


def _by_value(offer: OfferFacts) -> tuple[Decimal, int, str]:
    """Цена за единицу надёжности: рубли, делённые на вероятность в срок.

    Значения требуют решения человека (CLAUDE.md §7, пункт 8) и заданы
    как рабочее приближение. Деление в ``Decimal``, а не в ``float``:
    вероятность приходит ``Decimal`` из базы, и смешивать типы в сравнении
    цен нельзя. Нулевая вероятность отсеяна выше — делить на неё
    не приходится.
    """
    probability = offer.on_time_probability or Decimal(1)
    return (
        Decimal(offer.total.amount_minor) / probability,
        offer.total.amount_minor,
        str(offer.offer_id),
    )
