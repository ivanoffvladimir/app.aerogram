"""Расшифровка стоимости доходит от адаптера до ответа API.

Модульный тест (``tests/unit/test_cost_components.py``) проверяет разбор.
Здесь проверяется путь целиком — тот самый, который был разорван: адаптеры
считали расшифровку, а ``cost_components`` не заполнялись ни одной строкой
кода, и поле контракта всегда было пустым списком.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from aerogram.carriers import registry
from aerogram.shared.money import Money
from tests.integration.conftest import RATE_REQUEST, FakeCarrier

pytestmark = pytest.mark.asyncio

#: Расшифровка в духе ответа Почты России: подписи русские и её собственные,
#: среди них — надбавка за негабарит, ради которой всё и затевалось.
BREAKDOWN = {
    "Пересылка": Money(210_050, "RUB"),
    "Надбавка за негабарит": Money(35_000, "RUB"),
    "Уведомление о вручении": Money(0, "RUB"),
}


async def _rate(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post("/v1/rates", json=RATE_REQUEST, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def carrier_with_breakdown(carrier_setup: tuple[Any, Any]) -> FakeCarrier:
    adapter = FakeCarrier("fake", breakdown=BREAKDOWN)
    registry.register(adapter)
    return adapter


class TestBreakdownReachesTheResponse:
    async def test_the_carrier_labels_arrive(
        self, client: AsyncClient, headers: dict[str, str], carrier_with_breakdown: FakeCarrier
    ) -> None:
        """Надбавка за негабарит названа перевозчиком и обязана дойти как есть.

        Ради неё человек и просил показывать негабарит на экране: платформа
        его не вычисляет, перевозчик считает сам и присылает строкой.
        """
        body = await _rate(client, headers)
        offer = next(o for o in body["offers"] if o["total_cost"]["amount_minor"] == 245_050)

        labels = [c["description"] for c in offer["cost_components"]]
        assert "Надбавка за негабарит" in labels

    async def test_a_zero_component_does_not_arrive(
        self, client: AsyncClient, headers: dict[str, str], carrier_with_breakdown: FakeCarrier
    ) -> None:
        """Нулевая строка читалась бы как «услуга есть», хотя её нет."""
        body = await _rate(client, headers)
        offer = next(o for o in body["offers"] if o["total_cost"]["amount_minor"] == 245_050)
        assert "Уведомление о вручении" not in [c["description"] for c in offer["cost_components"]]

    async def test_the_order_is_by_amount_and_stable(
        self, client: AsyncClient, headers: dict[str, str], carrier_with_breakdown: FakeCarrier
    ) -> None:
        """Порядок задаётся суммой, а не выборкой из базы.

        Идентификаторы UUIDv7 здесь не помогут: все строки предложения
        создаются в одну миллисекунду, а счётчика в ``shared.ids`` нет.
        """
        body = await _rate(client, headers)
        offer = next(o for o in body["offers"] if o["total_cost"]["amount_minor"] == 245_050)
        amounts = [c["money"]["amount_minor"] for c in offer["cost_components"]]
        assert amounts == sorted(amounts, reverse=True)
        assert [c["description"] for c in offer["cost_components"]] == [
            "Пересылка",
            "Надбавка за негабарит",
        ]

    async def test_the_reused_quote_carries_the_same_breakdown(
        self, client: AsyncClient, headers: dict[str, str], carrier_with_breakdown: FakeCarrier
    ) -> None:
        """Повтор выдачи (FR-1.6) читает те же строки из базы.

        Расшифровка, собранная только в памяти, из повтора бы исчезла —
        и второй ответ на тот же запрос отличался бы от первого, хотя
        FR-1.6 обещает ровно обратное.
        """
        first = await _rate(client, headers)
        second = await _rate(client, headers)
        assert second["quote_id"] == first["quote_id"], "выдача не переиспользовалась"

        def labels(body: dict[str, Any]) -> list[list[str]]:
            return [[c["description"] for c in o["cost_components"]] for o in body["offers"]]

        assert labels(second) == labels(first)

    async def test_a_carrier_without_a_breakdown_gives_an_empty_list(
        self, client: AsyncClient, headers: dict[str, str], carrier_setup: tuple[Any, Any]
    ) -> None:
        """СДЭК расшифровки не отдаёт вовсе, и это не ошибка.

        Пустой список — честный ответ «перевозчик не сказал»; кнопки
        расшифровки в кабинете при нём просто нет.
        """
        registry.register(FakeCarrier("fake"))
        body = await _rate(client, headers)
        assert body["offers"]
        assert all(o["cost_components"] == [] for o in body["offers"])
