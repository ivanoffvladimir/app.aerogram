"""Четыре стратегии выбора и объяснение рекомендации.

Тесты не поднимают ни базу, ни перевозчиков: стратегия — чистая функция
над снимком предложений, и это её главное свойство (ADR-0014).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aerogram.routing.explanation import alternatives_delta, build_facts, render
from aerogram.routing.strategies import OfferFacts, rank
from aerogram.shared.enums import RiskLevel, RoutingStrategy, ScoreConfidence
from aerogram.shared.ids import uuid7
from aerogram.shared.money import Money

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def offer(
    *,
    price: int,
    days: int = 3,
    probability: str | None = None,
    risk: RiskLevel | None = None,
    eligible: bool = True,
    margin: int | None = None,
) -> OfferFacts:
    return OfferFacts(
        offer_id=uuid7(),
        carrier_id=uuid7(),
        total=Money(price, "RUB"),
        eta=BASE + timedelta(days=days),
        eligible=eligible,
        on_time_probability=Decimal(probability) if probability else None,
        risk=risk,
        deadline_margin_seconds=margin,
    )


class TestCheapest:
    def test_picks_the_lowest_total_cost(self) -> None:
        cheap, pricey = offer(price=100_000), offer(price=300_000)
        assert rank([pricey, cheap], RoutingStrategy.CHEAPEST).best is cheap

    def test_ties_are_broken_by_speed(self) -> None:
        """Одинаковая цена — берём то, что быстрее: иначе порядок случаен."""
        slow, fast = offer(price=100_000, days=5), offer(price=100_000, days=2)
        assert rank([slow, fast], RoutingStrategy.CHEAPEST).best is fast


class TestFastest:
    def test_picks_the_earliest_eta(self) -> None:
        slow, fast = offer(price=100_000, days=7), offer(price=900_000, days=1)
        assert rank([slow, fast], RoutingStrategy.FASTEST).best is fast

    def test_unknown_eta_never_wins(self) -> None:
        """Вариант без известного срока не может быть «самым быстрым»."""
        known = offer(price=900_000, days=9)
        unknown = OfferFacts(
            offer_id=uuid7(),
            carrier_id=uuid7(),
            total=Money(100_000, "RUB"),
            eta=None,
            eligible=True,
        )
        assert rank([unknown, known], RoutingStrategy.FASTEST).best is known


class TestReliable:
    def test_picks_the_highest_on_time_probability(self) -> None:
        weak = offer(price=100_000, probability="0.70")
        strong = offer(price=500_000, probability="0.97")
        assert rank([weak, strong], RoutingStrategy.RELIABLE).best is strong

    def test_without_data_nobody_is_more_reliable(self) -> None:
        """Нейтральная оценка одинакова для всех, поэтому решает цена."""
        cheap, pricey = offer(price=100_000), offer(price=500_000)
        assert rank([pricey, cheap], RoutingStrategy.RELIABLE).best is cheap


class TestOptimal:
    def test_prefers_cheaper_when_everything_else_is_equal(self) -> None:
        cheap, pricey = offer(price=100_000), offer(price=500_000)
        assert rank([pricey, cheap], RoutingStrategy.OPTIMAL).best is cheap

    def test_a_dearer_offer_can_win_on_time_and_reliability(self) -> None:
        """Цена не является жёстким потолком (продуктовое ТЗ, раздел 7).

        Дешёвый вариант идёт девять дней с низкой надёжностью и высоким риском,
        дорогой — сутки с высокой. Рекомендовать первый значило бы экономить
        деньги ценой срыва срока.
        """
        cheap = offer(price=100_000, days=9, probability="0.55", risk=RiskLevel.HIGH)
        pricey = offer(price=160_000, days=1, probability="0.98", risk=RiskLevel.LOW)
        assert rank([cheap, pricey], RoutingStrategy.OPTIMAL).best is pricey

    def test_price_still_wins_when_the_gap_is_large(self) -> None:
        """Обратная проверка: превосходство по сроку не оправдывает любую цену."""
        cheap = offer(price=100_000, days=4, probability="0.90", risk=RiskLevel.LOW)
        pricey = offer(price=5_000_000, days=1, probability="0.98", risk=RiskLevel.LOW)
        assert rank([cheap, pricey], RoutingStrategy.OPTIMAL).best is cheap

    def test_identical_offers_do_not_crash_on_normalisation(self) -> None:
        """Одинаковые значения дают деление на ноль при наивной нормализации."""
        a, b = offer(price=100_000), offer(price=100_000)
        ranking = rank([a, b], RoutingStrategy.OPTIMAL)
        assert len(ranking.ordered) == 2


class TestUnknownEta:
    def test_one_offer_without_a_date_does_not_erase_the_time_weight(self) -> None:
        """Условно огромный срок растянул бы шкалу и обнулил вес времени.

        Цена и надёжность у всех трёх одинаковы, поэтому решает только срок.
        Если предложение без даты подставляет в шкалу число размером с века,
        разница между девятью днями и одним схлопывается в ноль, и порядок
        становится произвольным.
        """
        slow = offer(price=100_000, days=9, probability="0.9", risk=RiskLevel.LOW)
        fast = offer(price=100_000, days=1, probability="0.9", risk=RiskLevel.LOW)
        undated = OfferFacts(
            offer_id=uuid7(),
            carrier_id=uuid7(),
            total=Money(100_000, "RUB"),
            eta=None,
            eligible=True,
            on_time_probability=Decimal("0.9"),
            risk=RiskLevel.LOW,
        )

        ranking = rank([slow, undated, fast], RoutingStrategy.OPTIMAL)
        assert ranking.best is fast
        # И медленный обязан стоять выше того, о чьём сроке ничего не известно.
        assert [o.offer_id for o in ranking.ordered] == [
            fast.offer_id,
            slow.offer_id,
            undated.offer_id,
        ]

    def test_offer_without_a_date_ranks_last_on_time(self) -> None:
        undated = OfferFacts(
            offer_id=uuid7(),
            carrier_id=uuid7(),
            total=Money(100_000, "RUB"),
            eta=None,
            eligible=True,
        )
        dated = offer(price=100_000, days=30)
        assert rank([undated, dated], RoutingStrategy.OPTIMAL).best is dated

    def test_all_dates_unknown_leaves_the_choice_to_price(self) -> None:
        cheap = OfferFacts(
            offer_id=uuid7(),
            carrier_id=uuid7(),
            total=Money(100_000, "RUB"),
            eta=None,
            eligible=True,
        )
        pricey = OfferFacts(
            offer_id=uuid7(),
            carrier_id=uuid7(),
            total=Money(500_000, "RUB"),
            eta=None,
            eligible=True,
        )
        assert rank([pricey, cheap], RoutingStrategy.OPTIMAL).best is cheap


class TestHardConstraints:
    def test_ineligible_offers_are_never_recommended(self) -> None:
        """Нарушивший жёсткое ограничение не побеждает ни при какой оценке."""
        late_and_cheap = offer(price=10_000, eligible=False)
        fits = offer(price=900_000)
        assert rank([late_and_cheap, fits], RoutingStrategy.CHEAPEST).best is fits

    def test_nothing_eligible_gives_no_recommendation(self) -> None:
        """Подставить «лучший из непригодных» значило бы выдать нарушение
        дедлайна за совет."""
        ranking = rank([offer(price=10_000, eligible=False)], RoutingStrategy.OPTIMAL)
        assert ranking.best is None
        assert ranking.ordered == ()


class TestConfidence:
    def test_no_data_means_low_confidence(self) -> None:
        """Система не изображает точность, которой у неё нет."""
        ranking = rank([offer(price=100_000)], RoutingStrategy.OPTIMAL)
        assert ranking.confidence is ScoreConfidence.LOW

    def test_partial_data_means_medium(self) -> None:
        offers = [offer(price=100_000, probability="0.9"), offer(price=200_000)]
        assert rank(offers, RoutingStrategy.OPTIMAL).confidence is ScoreConfidence.MEDIUM

    def test_full_data_means_high(self) -> None:
        offers = [
            offer(price=100_000, probability="0.9"),
            offer(price=200_000, probability="0.8"),
        ]
        assert rank(offers, RoutingStrategy.OPTIMAL).confidence is ScoreConfidence.HIGH


class TestExplanation:
    def test_facts_carry_codes_not_sentences(self) -> None:
        """В базу уезжают факты: текст интерфейс собирает сам."""
        ranking = rank([offer(price=100_000, margin=64_800)], RoutingStrategy.OPTIMAL)
        codes = [f.code for f in build_facts(ranking, RoutingStrategy.OPTIMAL)]
        assert "strategy" in codes
        assert "fits_deadline" in codes
        assert "confidence" in codes

    def test_cost_difference_is_explained_when_a_dearer_offer_wins(self) -> None:
        cheap = offer(price=100_000, days=9, probability="0.55", risk=RiskLevel.HIGH)
        pricey = offer(price=160_000, days=1, probability="0.98", risk=RiskLevel.LOW)
        ranking = rank([cheap, pricey], RoutingStrategy.OPTIMAL)

        facts = {f.code: f.params for f in build_facts(ranking, RoutingStrategy.OPTIMAL)}
        assert facts["costs_more_than_cheapest"]["amount_minor"] == 60_000
        assert facts["costs_more_than_cheapest"]["currency"] == "RUB"

    def test_rendering_produces_russian_lines(self) -> None:
        ranking = rank([offer(price=100_000, margin=64_800)], RoutingStrategy.CHEAPEST)
        lines = render([f.as_json() for f in build_facts(ranking, RoutingStrategy.CHEAPEST)])
        assert "Самый дешёвый вариант" in lines
        assert any("запас" in line for line in lines)

    def test_unknown_fact_is_skipped_not_fatal(self) -> None:
        """Незнакомый код не должен ломать экран (фронт-ТЗ, раздел 3)."""
        assert render([{"code": "invented_later", "value": 1}]) == []

    def test_single_offer_has_nothing_to_compare_with(self) -> None:
        ranking = rank([offer(price=100_000)], RoutingStrategy.OPTIMAL)
        assert alternatives_delta(ranking) == {}

    def test_delta_names_the_cheapest_alternative(self) -> None:
        cheap = offer(price=100_000, days=9, probability="0.55", risk=RiskLevel.HIGH)
        pricey = offer(price=160_000, days=1, probability="0.98", risk=RiskLevel.LOW)
        delta = alternatives_delta(rank([cheap, pricey], RoutingStrategy.OPTIMAL))
        assert delta["vs_cheapest_eligible"] == {"amount_minor": 60_000, "currency": "RUB"}
