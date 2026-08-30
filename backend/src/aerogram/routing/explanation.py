"""Объяснение рекомендации: факты в базу, текст в ответ.

Контракт (``Recommendation.explanation`` в openapi.yaml) — массив строк.
Системное ТЗ, раздел 9, требует хранить структурированные факты, а не готовый
текст, чтобы интерфейс мог локализовать объяснение и чтобы по нему можно было
считать аналитику.

Противоречия здесь нет, если разделить хранение и представление: в JSONB
уезжают факты, в ответ API — собранные из них строки. Переформулировать
объяснение задним числом можно будет без миграции данных.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aerogram.routing.strategies import Ranking
from aerogram.shared.enums import RoutingStrategy
from aerogram.shared.money import Money, format_ru

__all__ = ["ExplanationFact", "alternatives_delta", "build_facts", "render"]


@dataclass(frozen=True, slots=True)
class ExplanationFact:
    """Один факт объяснения: код и параметры, без текста."""

    code: str
    params: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"code": self.code, **self.params}


def build_facts(ranking: Ranking, strategy: RoutingStrategy) -> list[ExplanationFact]:
    """Собрать факты, объясняющие рекомендацию.

    Объясняется не «почему этот вариант хорош вообще», а чем он отличается
    от альтернатив: оператору нужно понять выбор, а не прочитать характеристику.
    """
    best = ranking.best
    if best is None:
        return [ExplanationFact("no_eligible_offers", {})]

    facts = [ExplanationFact("strategy", {"strategy": strategy.value})]

    if best.deadline_margin_seconds is not None:
        facts.append(
            ExplanationFact("fits_deadline", {"margin_seconds": best.deadline_margin_seconds})
        )

    cheapest = min(ranking.ordered, key=lambda o: o.total.amount_minor)
    if cheapest.offer_id != best.offer_id:
        # Разница считается вычитанием Money: валюты обязаны совпасть,
        # и попытка сравнить рубли с юанями упадёт здесь, а не в отчёте.
        delta = best.total - cheapest.total
        facts.append(
            ExplanationFact(
                "costs_more_than_cheapest",
                {"amount_minor": delta.amount_minor, "currency": delta.currency},
            )
        )
    else:
        facts.append(ExplanationFact("is_cheapest_eligible", {}))

    if best.on_time_probability is not None:
        facts.append(
            ExplanationFact("on_time_probability", {"value": str(best.on_time_probability)})
        )
    if best.risk is not None:
        facts.append(ExplanationFact("risk", {"level": best.risk.value}))

    facts.append(ExplanationFact("confidence", {"level": ranking.confidence.value}))
    return facts


def alternatives_delta(ranking: Ranking) -> dict[str, Any]:
    """Насколько рекомендованный вариант отличается от крайних альтернатив.

    Пусто, когда сравнивать не с чем: единственный вариант не отличается
    ни от чего, и писать «дороже на ноль» значит засорять снимок.
    """
    best = ranking.best
    if best is None or len(ranking.ordered) < 2:
        return {}

    cheapest = min(ranking.ordered, key=lambda o: o.total.amount_minor)
    delta: dict[str, Any] = {}

    if cheapest.offer_id != best.offer_id:
        difference = best.total - cheapest.total
        delta["vs_cheapest_eligible"] = {
            "amount_minor": difference.amount_minor,
            "currency": difference.currency,
        }

    probabilities = [o.on_time_probability for o in ranking.ordered if o.on_time_probability]
    if best.on_time_probability is not None and probabilities:
        best_available = max(probabilities)
        delta["on_time_probability_delta"] = str(best.on_time_probability - best_available)

    return delta


_STRATEGY_NAMES = {
    "optimal": "Оптимальный вариант",
    "cheapest": "Самый дешёвый вариант",
    "fastest": "Самый быстрый вариант",
    "reliable": "Самый надёжный вариант",
}
_RISK_NAMES = {"low": "низкий", "medium": "средний", "high": "высокий"}
_CONFIDENCE_NAMES = {"low": "низкая", "medium": "средняя", "high": "высокая"}

#: Шаблоны на русском. Интерфейс волен собрать свои из тех же фактов.
_TEMPLATES: dict[str, Callable[[dict[str, Any]], str]] = {
    "strategy": lambda p: _STRATEGY_NAMES.get(p["strategy"], p["strategy"]),
    "fits_deadline": lambda p: f"Укладывается в срок, запас {_hours(p['margin_seconds'])}",
    "costs_more_than_cheapest": lambda p: f"Дороже самого дешёвого подходящего на {_money(p)}",
    "is_cheapest_eligible": lambda _: "Самый дешёвый из подходящих",
    "on_time_probability": lambda p: (
        f"Вероятность доставки в срок {round(Decimal(p['value']) * 100)}%"
    ),
    "risk": lambda p: f"Риск: {_RISK_NAMES.get(p['level'], p['level'])}",
    "confidence": lambda p: f"Уверенность оценки: {_CONFIDENCE_NAMES.get(p['level'], p['level'])}",
    "no_eligible_offers": lambda _: "Подходящих вариантов нет",
}


def render(facts: list[dict[str, Any]]) -> list[str]:
    """Факты → строки ответа. Неизвестный код пропускается, а не ломает экран."""
    lines: list[str] = []
    for fact in facts:
        template = _TEMPLATES.get(str(fact.get("code")))
        if template is None:
            continue
        params = {k: v for k, v in fact.items() if k != "code"}
        lines.append(template(params))
    return lines


def _hours(seconds: int) -> str:
    """Запас в часах: секунды оператору ничего не говорят."""
    hours = seconds // 3600
    if hours >= 24:
        days = hours // 24
        return f"{days} сут."
    return f"{hours} ч."


def _money(params: dict[str, Any]) -> str:
    """Сумма по-русски: объяснение читает оператор, а не разработчик."""
    return format_ru(Money(int(params["amount_minor"]), str(params["currency"])))
