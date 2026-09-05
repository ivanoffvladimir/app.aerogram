"""Свод Carrier Score по платформе: что попадает в число, которое видят все.

Здесь проверяется не арифметика, а два обещания, нарушение которых
не падает и не видно на экране.

Первое — **порог обезличенности**. Свод по одному-двум клиентам это их
статистика, выданная остальным: ровно та утечка, из-за которой появилась
ADR-0017. Второе — **денег в своде нет**: тариф коммерческая тайна клиента,
и усреднённый по платформе он рассказал бы соседям об уровне чужих договоров.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from aerogram.intelligence.platform import accumulate, prior_from_totals
from aerogram.intelligence.repository import Observations
from aerogram.intelligence.score import (
    MIN_PLATFORM_SAMPLE,
    MIN_PLATFORM_TENANTS,
    PlatformPrior,
)
from aerogram.shared.ids import uuid7

CDEK = uuid7()
PECOM = uuid7()


def observed(carrier_id: UUID, **counters: int) -> Observations:
    """Наблюдения одного тенанта по одному перевозчику."""
    defaults = {
        "finalized": 10,
        "with_deadline": 10,
        "on_time": 9,
        "broken": 0,
        "with_incident": 0,
        "transparent": 8,
    }
    defaults.update(counters)
    return Observations(carrier_id=carrier_id, median_cost_minor=100_000, **defaults)  # type: ignore[arg-type]


class TestAccumulate:
    def test_counters_are_summed_not_averaged(self) -> None:
        """Среднее долей приравняло бы клиента с тремя отправлениями
        к клиенту с тремя тысячами: один неудачный месяц маленького
        испортил бы оценку перевозчика для всех."""
        totals = accumulate(
            [
                [observed(CDEK, finalized=1000, with_deadline=1000, on_time=900)],
                [observed(CDEK, finalized=10, with_deadline=10, on_time=0)],
                [observed(CDEK, finalized=10, with_deadline=10, on_time=10)],
            ]
        )
        assert len(totals) == 1
        assert totals[0].finalized == 1020
        assert totals[0].on_time == 910
        # Среднее долей дало бы (0.9 + 0 + 1) / 3 ≈ 0.63 вместо 0.89.
        assert prior_from_totals(totals[0]).on_time > Decimal("0.88")

    def test_tenants_are_counted_not_rows(self) -> None:
        totals = {row.carrier_id: row for row in accumulate([[observed(CDEK)] for _ in range(4)])}
        assert totals[CDEK].tenants == 4

    def test_a_tenant_without_shipments_does_not_count_towards_anonymity(self) -> None:
        """Иначе порог набирался бы теми, кто этим перевозчиком не возил,
        и свод по одному клиенту прошёл бы как обезличенный."""
        totals = {
            row.carrier_id: row
            for row in accumulate(
                [
                    [observed(CDEK, finalized=50, with_deadline=50, on_time=45)],
                    [observed(CDEK, finalized=0, with_deadline=0, on_time=0)],
                    [observed(PECOM, finalized=40)],
                ]
            )
        }
        assert totals[CDEK].tenants == 1
        assert totals[CDEK].is_publishable is False

    def test_carriers_are_kept_apart(self) -> None:
        """Свод, общий на всех перевозчиков, не даёт рейтинга вовсе."""
        totals = {
            row.carrier_id: row
            for row in accumulate([[observed(CDEK, on_time=10), observed(PECOM, on_time=1)]])
        }
        assert prior_from_totals(totals[CDEK]).on_time > prior_from_totals(totals[PECOM]).on_time


class TestAnonymityThreshold:
    def test_two_tenants_are_not_enough(self) -> None:
        """При двух каждый вычитает себя из агрегата и получает статистику
        второго почти точно."""
        totals = accumulate([[observed(CDEK, finalized=500)] for _ in range(2)])
        assert totals[0].tenants == 2
        assert totals[0].is_publishable is False

    def test_three_tenants_with_enough_shipments_pass(self) -> None:
        totals = accumulate([[observed(CDEK, finalized=100)] for _ in range(3)])
        assert totals[0].is_publishable is True

    def test_three_tenants_with_too_few_shipments_do_not_pass(self) -> None:
        """Трёх клиентов с одним отправлением каждый достаточно для
        анонимности, но не для утверждения о перевозчике."""
        totals = accumulate([[observed(CDEK, finalized=1, with_deadline=1)] for _ in range(3)])
        assert totals[0].tenants == 3
        assert totals[0].finalized < MIN_PLATFORM_SAMPLE
        assert totals[0].is_publishable is False

    def test_the_thresholds_are_the_ones_that_were_decided(self) -> None:
        """Порог — решение человека от 5 сентября 2026 (ADR-0026). Тест
        стоит здесь затем, чтобы его снижение было видно в диффе."""
        assert MIN_PLATFORM_TENANTS == 3
        assert MIN_PLATFORM_SAMPLE == 30


class TestNoMoneyInTheBaseline:
    def test_price_index_stays_neutral(self) -> None:
        """Тариф — коммерческая тайна клиента. Усреднённый по платформе,
        он рассказал бы соседям об уровне чужих договоров."""
        totals = accumulate([[observed(CDEK, finalized=100)] for _ in range(3)])
        assert prior_from_totals(totals[0]).price_index == PlatformPrior().price_index

    def test_the_totals_carry_no_cost_field(self) -> None:
        """Проверяется состав, а не значение: поле, добавленное «на будущее»,
        утечёт ровно тогда, когда его кто-нибудь заполнит."""
        assert not [name for name in accumulate([[observed(CDEK)]])[0].__slots__ if "cost" in name]


class TestUnobservedIsNotZero:
    def test_a_component_nobody_measured_keeps_its_default(self) -> None:
        """«Не измеряли» не равно «плохо»: подставить сюда ноль значило бы
        наказать перевозчика за то, что клиенты не ставили дедлайн."""
        totals = accumulate(
            [
                [observed(CDEK, finalized=50, with_deadline=0, on_time=0)]
                for _ in range(MIN_PLATFORM_TENANTS)
            ]
        )
        prior = prior_from_totals(totals[0])
        assert prior.on_time == PlatformPrior().on_time
        # Остальное измерено: срывов не было, и это уже утверждение.
        assert prior.reliability == Decimal(1)

    def test_a_measured_zero_is_not_a_default(self) -> None:
        """Обратная сторона того же правила: перевозчик, у которого срок
        сорван у всех, обязан отличаться от того, кого не мерили."""
        totals = accumulate(
            [
                [observed(CDEK, finalized=50, with_deadline=50, on_time=0)]
                for _ in range(MIN_PLATFORM_TENANTS)
            ]
        )
        assert prior_from_totals(totals[0]).on_time == Decimal(0)
