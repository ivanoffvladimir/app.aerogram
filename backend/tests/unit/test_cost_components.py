"""Расшифровка цены перевозчика → строки ``cost_components``.

Её считают три адаптера, и до сих пор она не доходила никуда: поле контракта
``RateOffer.cost_components`` всегда было пустым списком, а вместе с ним
терялась и надбавка за негабарит, которую Почта России называет прямо
и уже включает в итог.
"""

from __future__ import annotations

from datetime import date

import pytest

from aerogram.carriers.base import Quote
from aerogram.rating.service import _components
from aerogram.shared.enums import CostComponentType, PriceSource
from aerogram.shared.ids import uuid7
from aerogram.shared.money import Money

TENANT = uuid7()


def quote(breakdown: dict[str, Money], *, price: Money = Money(150_000, "RUB")) -> Quote:
    return Quote(
        service_code="136",
        tariff_code="136",
        service_name="Посылка",
        price=price,
        transit_days_min=2,
        transit_days_max=3,
        promised_delivery_date=date(2026, 9, 8),
        price_source=PriceSource.OWN_CONTRACT,
        price_breakdown=breakdown,
    )


class TestComponents:
    def test_the_carrier_label_survives(self) -> None:
        """Подпись перевозчика — единственное, что несёт смысл строки.

        Тип у всех строк ``other``, пока тип не назовёт сам адаптер
        (правка ``carriers/base.py``, CLAUDE.md §7). Значит подпись обязана
        доходить до экрана дословно.
        """
        rows = _components(quote({"Надбавка за негабарит": Money(35_000, "RUB")}), TENANT)
        assert [(r.type, r.description, r.amount_minor) for r in rows] == [
            (CostComponentType.OTHER, "Надбавка за негабарит", 35_000)
        ]

    def test_the_id_is_not_an_ordering_key(self) -> None:
        """UUIDv7 сортируем по времени только между миллисекундами.

        Все строки одного предложения создаются в одну миллисекунду, а
        счётчика в ``shared.ids`` нет — младшие биты случайны. Порядок по
        идентификатору здесь означал бы расшифровку, переставляющуюся
        от показа к показу без единого изменения данных.

        Тест грубый намеренно: он не требует, чтобы порядок сломался
        (случайность может и совпасть), а фиксирует, что на него нельзя
        опираться — порядок задаётся в другом месте и по другому ключу.
        """
        breakdown = {f"Строка {index}": Money(1_000 + index, "RUB") for index in range(20)}
        rows = _components(quote(breakdown, price=Money(100_000, "RUB")), TENANT)
        by_id = [r.description for r in sorted(rows, key=lambda r: r.id)]
        by_amount = [
            r.description
            for r in sorted(rows, key=lambda r: (-r.amount_minor, r.description or ""))
        ]
        assert by_amount == [f"Строка {index}" for index in range(19, -1, -1)]
        assert set(by_id) == set(by_amount)

    def test_a_zero_component_is_not_written(self) -> None:
        """«Надбавка за негабарит — 0 ₽» читается как «надбавка есть»."""
        rows = _components(
            quote(
                {
                    "Пересылка": Money(150_000, "RUB"),
                    "Надбавка за негабарит": Money(0, "RUB"),
                }
            ),
            TENANT,
        )
        assert [r.description for r in rows] == ["Пересылка"]

    def test_a_component_in_another_currency_is_refused(self) -> None:
        """Сложить её с ценой нельзя, а показать рядом — обмануть итогом."""
        rows = _components(
            quote(
                {
                    "Пересылка": Money(150_000, "RUB"),
                    "Сбор": Money(900, "KZT"),
                },
            ),
            TENANT,
        )
        assert [r.description for r in rows] == ["Пересылка"]

    def test_a_long_carrier_label_is_trimmed(self) -> None:
        """У ПЭК подпись — свободный текст поля ``info``, и он попадает на экран."""
        rows = _components(quote({"П" * 500: Money(1_000, "RUB")}), TENANT)
        assert rows[0].description is not None
        assert len(rows[0].description) == 200

    def test_an_empty_breakdown_gives_no_rows(self) -> None:
        """СДЭК расшифровки не отдаёт вовсе — это не ошибка, а её отсутствие."""
        assert _components(quote({}), TENANT) == []

    def test_the_tenant_is_stamped_on_every_row(self) -> None:
        """Таблица под RLS: строка без тенанта не сохранится и не должна."""
        rows = _components(quote({"Пересылка": Money(1, "RUB")}), TENANT)
        assert [r.tenant_id for r in rows] == [TENANT]


@pytest.mark.parametrize(
    "breakdown",
    [
        {"Пересылка": Money(100_000, "RUB"), "Надбавка": Money(50_000, "RUB")},
        {"Пересылка": Money(150_000, "RUB")},
    ],
)
def test_components_never_exceed_the_price(breakdown: dict[str, Money]) -> None:
    """Расшифровка не должна превышать итог, иначе она объясняет не эту цену.

    Проверка нестрогая намеренно: у части перевозчиков сумма составляющих
    МЕНЬШЕ итога — они перечисляют не всё. Больше итога она быть не может
    ни у кого, и это ловит ошибку разбора у адаптера.
    """
    rows = _components(quote(breakdown), TENANT)
    assert sum(r.amount_minor for r in rows) <= 150_000
