"""Ранжирование выдачи (FR-5.3, FR-5.4).

Чистая функция без базы: ранг — это предметное правило, и проверять его
на поднятом PostgreSQL значило бы прятать правило за инфраструктурой.
"""

from __future__ import annotations

from datetime import date

from aerogram.rating.models import RateQuote
from aerogram.rating.service import rank_quotes
from aerogram.shared.ids import uuid7
from aerogram.shared.money import Money


def _quote(
    price: str,
    days: int,
    *,
    meets: bool | None = None,
    promised: date | None = None,
) -> RateQuote:
    return RateQuote(
        id=uuid7(),
        tenant_id=uuid7(),
        rate_request_id=uuid7(),
        carrier_id=uuid7(),
        price_amount_minor=Money.from_major(price, "RUB").amount_minor,
        currency="RUB",
        transit_days_min=days,
        transit_days_max=days,
        promised_delivery_date=promised,
        meets_deadline=meets,
        expires_at=None,
    )


class TestCombinedRank:
    def test_cheapest_and_fastest_wins(self) -> None:
        cheap_fast = _quote("1000", 2)
        expensive_slow = _quote("3000", 5)
        rank_quotes([expensive_slow, cheap_fast])

        assert cheap_fast.rank == 1
        assert expensive_slow.rank == 2

    def test_price_outweighs_transit_time(self) -> None:
        """Вес цены 0,4 против срока 0,3 (FR-5.3).

        При равном разбросе дешевле важнее быстрее — это и есть отличие
        комбинированного ранга от сортировки по сроку.
        """
        cheap_slow = _quote("1000", 5)
        expensive_fast = _quote("3000", 2)
        rank_quotes([expensive_fast, cheap_slow])

        assert cheap_slow.rank == 1

    def test_ranks_are_dense_and_start_at_one(self) -> None:
        quotes = [_quote("1000", 2), _quote("2000", 3), _quote("3000", 4)]
        rank_quotes(quotes)
        assert sorted(q.rank for q in quotes) == [1, 2, 3]

    def test_identical_quotes_still_get_distinct_ranks(self) -> None:
        # Одинаковый ранг у двух строк сделал бы порядок выдачи случайным.
        quotes = [_quote("1000", 2), _quote("1000", 2)]
        rank_quotes(quotes)
        assert sorted(q.rank for q in quotes) == [1, 2]

    def test_single_quote_gets_rank_one(self) -> None:
        quote = _quote("1000", 2)
        rank_quotes([quote])
        assert quote.rank == 1

    def test_empty_list_does_not_fail(self) -> None:
        rank_quotes([])


class TestDeadline:
    def test_missing_the_deadline_pushes_a_quote_down_but_not_out(self) -> None:
        """FR-5.4: не уложившиеся помечаются и уходят вниз, но не скрываются.

        Скрыть их нельзя: иногда единственный доступный вариант — опоздать,
        и решение принимает человек, а не система.
        """
        cheap_late = _quote("500", 9, meets=False)
        pricey_on_time = _quote("5000", 2, meets=True)
        rank_quotes([cheap_late, pricey_on_time], required_deadline=True)

        assert pricey_on_time.rank == 1
        assert cheap_late.rank == 2

    def test_deadline_is_ignored_when_not_requested(self) -> None:
        # Без требуемой даты метка соответствия не влияет на порядок.
        cheap = _quote("500", 9, meets=False)
        pricey = _quote("5000", 2, meets=True)
        rank_quotes([cheap, pricey], required_deadline=False)

        assert cheap.rank == 1


class TestMissingData:
    def test_quotes_without_transit_days_are_ranked_by_price(self) -> None:
        cheap = _quote("1000", 0)
        cheap.transit_days_max = None
        pricey = _quote("2000", 0)
        pricey.transit_days_max = None
        rank_quotes([pricey, cheap])

        assert cheap.rank == 1

    def test_score_is_not_faked_when_absent(self) -> None:
        """Вес скора не перераспределяется на цену, пока скора нет.

        Подставить среднее вместо отсутствующего скора значило бы выдать
        выдумку за данные — тот же принцип, что и в разделе 10.2 ТЗ
        про холодный старт Carrier Score.
        """
        quotes = [_quote("1000", 2), _quote("2000", 4)]
        rank_quotes(quotes)
        assert all(q.score_at_quote is None for q in quotes)
