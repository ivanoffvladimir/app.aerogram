"""Формула Carrier Score (ТЗ, раздел 10).

Проверяется не «работает ли арифметика», а то, ради чего написан раздел 10.2:
холодный старт не должен превращаться в уверенные советы по трём наблюдениям.
Ошибка здесь не падает — она тихо меняет рекомендации, которые продукт даёт
клиенту, и обнаруживается по чужим убыткам.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aerogram.intelligence.score import (
    MIN_SAMPLE,
    PRIOR_WEIGHT,
    WEIGHTS,
    Components,
    PlatformPrior,
    basis_for,
    confidence_for,
    score_from,
    smooth,
)
from aerogram.shared.enums import ScoreBasis, ScoreConfidence

PERFECT = Components(
    on_time=Decimal(1),
    reliability=Decimal(1),
    incident_free=Decimal(1),
    price_index=Decimal(1),
    data_quality=Decimal(1),
)
AWFUL = Components(
    on_time=Decimal(0),
    reliability=Decimal(0),
    incident_free=Decimal(0),
    price_index=Decimal(0),
    data_quality=Decimal(0),
)


class TestWeights:
    def test_weights_sum_to_one(self) -> None:
        """«Почти единица» тихо сместила бы шкалу у всех перевозчиков."""
        assert sum(WEIGHTS.values()) == Decimal(1)

    def test_weights_match_the_spec(self) -> None:
        """Раздел 10.1: пункт за пунктом."""
        expected = {
            "on_time": Decimal("0.35"),
            "reliability": Decimal("0.20"),
            "incident_free": Decimal("0.20"),
            "price_index": Decimal("0.15"),
            "data_quality": Decimal("0.10"),
        }
        assert dict(WEIGHTS) == expected


class TestConfidence:
    @pytest.mark.parametrize(
        ("sample", "expected"),
        [
            (0, ScoreConfidence.INSUFFICIENT),
            (9, ScoreConfidence.INSUFFICIENT),
            (10, ScoreConfidence.LOW),
            (29, ScoreConfidence.LOW),
            (30, ScoreConfidence.MEDIUM),
            (99, ScoreConfidence.MEDIUM),
            (100, ScoreConfidence.HIGH),
        ],
    )
    def test_thresholds_follow_the_spec(self, sample: int, expected: ScoreConfidence) -> None:
        """FR-7.3, границы включительно. Считается по СВОЕЙ выборке."""
        assert confidence_for(sample) is expected

    def test_the_platform_baseline_does_not_raise_confidence(self) -> None:
        """Доверие отвечает «насколько это про нас», а свод посчитан
        по чужим договорам и чужим направлениям. Он делает оценку
        возможной, но не делает её более вашей."""
        assert confidence_for(0, has_baseline=True) is ScoreConfidence.LOW
        assert confidence_for(9, has_baseline=True) is ScoreConfidence.LOW
        assert confidence_for(100, has_baseline=True) is ScoreConfidence.HIGH

    def test_no_data_anywhere_is_insufficient(self) -> None:
        assert confidence_for(0) is ScoreConfidence.INSUFFICIENT


class TestBasis:
    @pytest.mark.parametrize(
        ("sample", "has_baseline", "expected"),
        [
            (0, False, ScoreBasis.NONE),
            (0, True, ScoreBasis.PLATFORM),
            (9, True, ScoreBasis.PLATFORM),
            (10, True, ScoreBasis.MIXED),
            (10, False, ScoreBasis.OWN),
            (500, False, ScoreBasis.OWN),
        ],
    )
    def test_basis_names_whose_data_it_is(
        self, sample: int, has_baseline: bool, expected: ScoreBasis
    ) -> None:
        assert basis_for(sample, has_baseline=has_baseline) is expected


class TestColdStart:
    """Раздел 10.2: чего стоит показать число слишком рано."""

    def test_without_a_baseline_a_small_sample_gives_no_number(self) -> None:
        """Ноль читается как «худший перевозчик», а он всего лишь новый."""
        result = score_from(PERFECT, MIN_SAMPLE - 1)
        assert result.score is None
        assert result.confidence is ScoreConfidence.INSUFFICIENT
        assert result.basis is ScoreBasis.NONE

    def test_a_small_sample_is_pulled_towards_the_baseline(self) -> None:
        """Десять безупречных доставок — ещё не сто баллов."""
        result = score_from(PERFECT, 10)
        assert result.confidence is ScoreConfidence.LOW
        # (1·10 + 0.5·20) / 30 = 0.666…
        assert result.score == 67

    def test_a_large_sample_reaches_its_own_value(self) -> None:
        """С ростом выборки база перестаёт мешать."""
        result = score_from(PERFECT, 2000)
        assert result.score is not None
        assert result.score >= 99

    def test_a_bad_carrier_is_not_slandered_on_a_small_sample_either(self) -> None:
        """Сглаживание работает в обе стороны: десять срывов — не ноль баллов."""
        assert score_from(AWFUL, 10).score == 33

    def test_more_data_separates_the_good_from_the_bad(self) -> None:
        """Иначе смысл скора теряется: он обязан различать перевозчиков."""
        good = score_from(PERFECT, 500).score
        bad = score_from(AWFUL, 500).score
        assert good is not None and bad is not None
        assert good - bad > 80


class TestPlatformBaseline:
    """Оценка есть до первого отправления клиента (ADR-0026).

    Ради этого свод и существует: клиент, впервые открывший кабинет, обязан
    видеть, чем перевозчики отличаются друг от друга, — иначе выбирать ему
    не из чего, и Decision Engine советует по одной цене.
    """

    def test_a_carrier_has_a_score_before_the_first_shipment(self) -> None:
        result = score_from(Components(), 0, _baseline(Decimal("0.9")))
        assert result.score is not None
        assert result.basis is ScoreBasis.PLATFORM
        # Доверие низкое: данные есть, но собраны не этим клиентом.
        assert result.confidence is ScoreConfidence.LOW

    def test_the_baseline_ranks_carriers_at_zero_own_sample(self) -> None:
        """Главное требование: рейтинг существует с первого дня.

        Приор, общий на всех перевозчиков, дал бы здесь одинаковые числа —
        ровно это и было сломано в версии формулы 1.0.0.
        """
        good = score_from(Components(), 0, _baseline(Decimal("0.95"))).score
        bad = score_from(Components(), 0, _baseline(Decimal("0.30"))).score
        assert good is not None and bad is not None
        assert good > bad

    def test_own_experience_displaces_the_platform_gradually(self) -> None:
        """Резкого переключения нет: скор не прыгает в день, когда набралась
        выборка. Прыжок означал бы, что вчерашний совет был неправдой."""
        baseline = _baseline(Decimal("0.9"))
        awful_own = Components(
            on_time=Decimal(0),
            reliability=Decimal(0),
            incident_free=Decimal(0),
            price_index=Decimal("0.5"),
            data_quality=Decimal(0),
        )
        steps = [score_from(awful_own, n, baseline).score for n in (0, 10, 50, 500)]
        assert all(step is not None for step in steps)
        # Каждый следующий шаг ближе к собственному (плохому) опыту.
        assert steps == sorted(steps, reverse=True)
        assert steps[0] != steps[-1]

    def test_the_basis_says_whose_data_it_is(self) -> None:
        baseline = _baseline(Decimal("0.9"))
        assert score_from(Components(), 0, baseline).basis is ScoreBasis.PLATFORM
        assert score_from(PERFECT, 5, baseline).basis is ScoreBasis.PLATFORM
        assert score_from(PERFECT, MIN_SAMPLE, baseline).basis is ScoreBasis.MIXED
        assert score_from(PERFECT, 500).basis is ScoreBasis.OWN

    def test_a_few_own_shipments_do_not_claim_to_be_own_data(self) -> None:
        """Три отправления при весе приора в двадцать сдвигают результат
        на седьмую часть. Называть это «своей оценкой» значит приписать
        клиенту вывод, которого его данные не поддерживают."""
        assert score_from(PERFECT, 3, _baseline(Decimal("0.5"))).basis is ScoreBasis.PLATFORM

    def test_without_a_baseline_and_without_data_there_is_no_score(self) -> None:
        """Перевозчик, которым не возил ещё никто на платформе."""
        result = score_from(Components(), 0)
        assert result.score is None
        assert result.basis is ScoreBasis.NONE


class TestSmoothing:
    def test_an_unobserved_component_keeps_the_prior(self) -> None:
        """Ноль вместо «не наблюдалось» наградил бы того, кого не считали."""
        assert smooth(None, Decimal("0.9"), 1000) == Decimal("0.9")

    def test_the_prior_weighs_exactly_twenty_observations(self) -> None:
        """Раздел 10.2: m = 20."""
        assert PRIOR_WEIGHT == 20
        # При выборке ровно в приорный вес собственные данные весят половину.
        assert smooth(Decimal(1), Decimal(0), PRIOR_WEIGHT) == Decimal("0.5")

    def test_a_missing_component_does_not_drag_the_score_down(self) -> None:
        """Перевозчик, по которому нет инцидентов в данных, не должен
        оказаться хуже того, у кого они посчитаны и равны нулю."""
        unknown = score_from(Components(on_time=Decimal(1)), 200).score
        counted = score_from(Components(on_time=Decimal(1), incident_free=Decimal(0)), 200).score
        assert unknown is not None and counted is not None
        assert unknown > counted


class TestScale:
    def test_the_score_never_leaves_zero_to_hundred(self) -> None:
        """Ограничение таблицы требует того же; выход за шкалу упал бы в базе."""
        broken = Components(on_time=Decimal(5), reliability=Decimal(-3))
        score = score_from(broken, 500).score
        assert score is not None
        assert 0 <= score <= 100

    def test_a_custom_platform_prior_is_used(self) -> None:
        """Приор берётся из данных платформы, когда они есть."""
        pessimistic = PlatformPrior(
            on_time=Decimal(0),
            reliability=Decimal(0),
            incident_free=Decimal(0),
            price_index=Decimal(0),
            data_quality=Decimal(0),
        )
        with_default = score_from(PERFECT, 10).score
        with_pessimistic = score_from(PERFECT, 10, pessimistic).score
        assert with_default is not None and with_pessimistic is not None
        assert with_pessimistic < with_default


def _baseline(quality: Decimal) -> PlatformPrior:
    """Платформенная база с одинаковым уровнем качества по всем компонентам.

    Цена намеренно остаётся серединой шкалы: денег в своде нет вовсе.
    """
    return PlatformPrior(
        on_time=quality,
        reliability=quality,
        incident_free=quality,
        price_index=Decimal("0.5"),
        data_quality=quality,
    )
