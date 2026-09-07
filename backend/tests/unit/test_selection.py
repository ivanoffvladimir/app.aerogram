"""Автовыбор: какое предложение берёт правило.

Тут проверяется то, что ошибётся молча. Выбор без человека попадает
в неизменяемый снимок, и «почему взяли это» через год объясняется только
воспроизведением: тот же снимок обязан дать тот же ответ.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aerogram.routing.selection import Abstention, select
from aerogram.routing.strategies import OfferFacts
from aerogram.shared.enums import SelectionRule
from aerogram.shared.ids import uuid7
from aerogram.shared.money import Money

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
CARRIER = uuid7()


def offer(
    *,
    minor: int = 100_000,
    eta: datetime | None = NOW + timedelta(days=3),
    eligible: bool = True,
    score: int | None = 80,
    probability: str | None = "0.9",
    margin: int | None = 86_400,
    currency: str = "RUB",
) -> OfferFacts:
    return OfferFacts(
        offer_id=uuid7(),
        carrier_id=CARRIER,
        total=Money(minor, currency),
        eta=eta,
        eligible=eligible,
        on_time_probability=Decimal(probability) if probability is not None else None,
        carrier_score=score,
        deadline_margin_seconds=margin,
    )


class TestCheapest:
    def test_takes_the_lowest_total(self) -> None:
        cheap = offer(minor=50_000)
        result = select([offer(minor=90_000), cheap], SelectionRule.CHEAPEST, deadline_set=False)
        assert result.offer is cheap

    def test_ineligible_offers_are_not_candidates(self) -> None:
        """Запрещённое политикой нельзя выбрать ни при какой оценке."""
        result = select(
            [offer(minor=10_000, eligible=False), offer(minor=90_000)],
            SelectionRule.CHEAPEST,
            deadline_set=False,
        )
        assert result.offer is not None
        assert result.offer.total.amount_minor == 90_000


class TestAbstention:
    def test_no_eligible_offers(self) -> None:
        result = select([offer(eligible=False)], SelectionRule.CHEAPEST, deadline_set=False)
        assert result.offer is None
        assert result.abstention is Abstention.NO_ELIGIBLE_OFFERS

    def test_empty_input(self) -> None:
        assert select([], SelectionRule.CHEAPEST, deadline_set=False).abstention is (
            Abstention.NO_ELIGIBLE_OFFERS
        )

    def test_mixed_currencies_stop_every_rule(self) -> None:
        """«Дешевле» не должно оказаться дешевле в тенге.

        Сравнить суммы разных валют нельзя (CLAUDE.md §6), а на автоматическом
        пути некому заметить подмену.
        """
        candidates = [offer(minor=50_000, currency="KZT"), offer(minor=90_000)]
        for rule in SelectionRule:
            result = select(candidates, rule, deadline_set=True)
            assert result.abstention is Abstention.MIXED_CURRENCIES, rule

    def test_fastest_without_a_single_known_eta(self) -> None:
        result = select([offer(eta=None)], SelectionRule.FASTEST, deadline_set=False)
        assert result.abstention is Abstention.NO_KNOWN_ETA

    def test_best_score_without_a_single_score(self) -> None:
        result = select([offer(score=None)], SelectionRule.BEST_SCORE, deadline_set=False)
        assert result.abstention is Abstention.NO_CARRIER_SCORES

    def test_best_value_without_a_single_probability(self) -> None:
        result = select([offer(probability=None)], SelectionRule.BEST_VALUE, deadline_set=False)
        assert result.abstention is Abstention.NO_ON_TIME_PROBABILITY

    def test_best_value_ignores_a_zero_probability(self) -> None:
        """Ноль в знаменателе — не «бесконечно дорого», а отсутствие данных."""
        result = select([offer(probability="0")], SelectionRule.BEST_VALUE, deadline_set=False)
        assert result.abstention is Abstention.NO_ON_TIME_PROBABILITY

    def test_deadline_rule_without_a_deadline(self) -> None:
        """Подменить его на «самый дешёвый» значит исполнить не то правило."""
        result = select([offer()], SelectionRule.CHEAPEST_MEETING_DEADLINE, deadline_set=False)
        assert result.abstention is Abstention.NO_DEADLINE_IN_REQUEST

    def test_deadline_rule_when_nobody_proves_the_margin(self) -> None:
        result = select(
            [offer(margin=None), offer(margin=-3_600)],
            SelectionRule.CHEAPEST_MEETING_DEADLINE,
            deadline_set=True,
        )
        assert result.abstention is Abstention.NO_PROVEN_DEADLINE_MARGIN


class TestRules:
    def test_fastest_skips_offers_without_an_eta(self) -> None:
        """Неизвестный срок не «самый быстрый», а неизвестность."""
        quick = offer(eta=NOW + timedelta(days=1))
        result = select([offer(eta=None), quick], SelectionRule.FASTEST, deadline_set=False)
        assert result.offer is quick

    def test_best_score_takes_the_highest(self) -> None:
        best = offer(score=95, minor=200_000)
        result = select([offer(score=60), best], SelectionRule.BEST_SCORE, deadline_set=False)
        assert result.offer is best

    def test_best_value_prefers_reliability_it_can_pay_for(self) -> None:
        """100 000 при 0.5 дороже 120 000 при 0.99 на единицу надёжности."""
        reliable = offer(minor=120_000, probability="0.99")
        result = select(
            [offer(minor=100_000, probability="0.5"), reliable],
            SelectionRule.BEST_VALUE,
            deadline_set=False,
        )
        assert result.offer is reliable

    def test_deadline_rule_takes_the_cheapest_that_fits(self) -> None:
        fits = offer(minor=90_000, margin=3_600)
        result = select(
            [offer(minor=50_000, margin=-1), fits],
            SelectionRule.CHEAPEST_MEETING_DEADLINE,
            deadline_set=True,
        )
        assert result.offer is fits

    def test_a_zero_margin_still_fits(self) -> None:
        """Ровно в срок — это в срок, а не мимо."""
        result = select(
            [offer(margin=0)], SelectionRule.CHEAPEST_MEETING_DEADLINE, deadline_set=True
        )
        assert result.offer is not None


class TestReproducibility:
    def test_a_tie_is_broken_by_offer_id_and_not_by_input_order(self) -> None:
        """Иначе тот же снимок через год назовёт другое предложение.

        Ничья по цене и сроку реальна: два тарифа одного перевозчика
        сплошь и рядом стоят одинаково.
        """
        first, second = offer(minor=70_000), offer(minor=70_000)
        forward = select([first, second], SelectionRule.CHEAPEST, deadline_set=False)
        backward = select([second, first], SelectionRule.CHEAPEST, deadline_set=False)
        assert forward.offer is not None and backward.offer is not None
        assert forward.offer.offer_id == backward.offer.offer_id

    @pytest.mark.parametrize("rule", list(SelectionRule))
    def test_every_rule_is_stable_under_reordering(self, rule: SelectionRule) -> None:
        candidates = [
            offer(minor=70_000, score=80, probability="0.9", margin=100),
            offer(minor=70_000, score=80, probability="0.9", margin=100),
            offer(minor=70_000, score=80, probability="0.9", margin=100),
        ]
        forward = select(candidates, rule, deadline_set=True)
        backward = select(list(reversed(candidates)), rule, deadline_set=True)
        assert forward.offer is not None and backward.offer is not None
        assert forward.offer.offer_id == backward.offer.offer_id

    def test_exactly_one_field_is_filled(self) -> None:
        """«Выбрал и воздержался» — состояние, которого не бывает."""
        for rule in SelectionRule:
            result = select([offer()], rule, deadline_set=True)
            assert (result.offer is None) != (result.abstention is None), rule
