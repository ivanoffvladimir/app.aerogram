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
    confidence_for,
    score_from,
    smooth,
)
from aerogram.shared.enums import ScoreConfidence

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
        """FR-7.3, границы включительно."""
        assert confidence_for(sample) is expected


class TestColdStart:
    def test_too_small_a_sample_gives_no_number_at_all(self) -> None:
        """Ноль читается как «худший перевозчик», а он всего лишь новый."""
        score, confidence = score_from(PERFECT, MIN_SAMPLE - 1)
        assert score is None
        assert confidence is ScoreConfidence.INSUFFICIENT

    def test_a_small_sample_is_pulled_towards_the_platform_mean(self) -> None:
        """Десять безупречных доставок — ещё не сто баллов."""
        score, confidence = score_from(PERFECT, 10)
        assert confidence is ScoreConfidence.LOW
        assert score is not None
        # (1·10 + 0.5·20) / 30 = 0.666…
        assert score == 67

    def test_a_large_sample_reaches_its_own_value(self) -> None:
        """С ростом выборки приор перестаёт мешать."""
        score, _ = score_from(PERFECT, 2000)
        assert score is not None
        assert score >= 99

    def test_a_bad_carrier_is_not_slandered_on_a_small_sample_either(self) -> None:
        """Сглаживание работает в обе стороны: десять срывов — не ноль баллов."""
        score, _ = score_from(AWFUL, 10)
        assert score == 33

    def test_more_data_separates_the_good_from_the_bad(self) -> None:
        """Иначе смысл скора теряется: он обязан различать перевозчиков."""
        good, _ = score_from(PERFECT, 500)
        bad, _ = score_from(AWFUL, 500)
        assert good is not None and bad is not None
        assert good - bad > 80


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
        unknown, _ = score_from(Components(on_time=Decimal(1)), 200)
        counted, _ = score_from(Components(on_time=Decimal(1), incident_free=Decimal(0)), 200)
        assert unknown is not None and counted is not None
        assert unknown > counted


class TestScale:
    def test_the_score_never_leaves_zero_to_hundred(self) -> None:
        """Ограничение таблицы требует того же; выход за шкалу упал бы в базе."""
        broken = Components(on_time=Decimal(5), reliability=Decimal(-3))
        score, _ = score_from(broken, 500)
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
        with_default, _ = score_from(PERFECT, 10)
        with_pessimistic, _ = score_from(PERFECT, 10, pessimistic)
        assert with_default is not None and with_pessimistic is not None
        assert with_pessimistic < with_default
