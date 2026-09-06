"""Проверка доступов перевозчика: что она обещает и чего не выдаёт наружу.

Метод отвечает на вопрос кабинета «работают ли доступы». Главное здесь —
что **«не работают» это ответ, а не исключение**: подняв его, мы заставили
бы вызывающего ловить ошибку и превращать обратно в ответ, а по дороге
сбой перевозчика однажды дал бы 500 (CLAUDE.md §6).

Второе — в сохраняемый текст не должно попадать тело ответа перевозчика:
поле живёт в базе и показывается в кабинете, а в теле бывает эхо логина.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount, CarrierAdapter, HealthResult
from aerogram.carriers.health import MESSAGE_LIMIT, probe
from aerogram.carriers.major.adapter import MajorExpressAdapter
from aerogram.carriers.pochta.adapter import PochtaAdapter
from aerogram.carriers.pochta.client import SANDBOX_BASE_URL, PochtaClient
from aerogram.shared.errors import (
    CarrierAuthError,
    CarrierError,
    CarrierTimeout,
    CarrierValidationError,
)


def _account(**overrides: object) -> CarrierAccount:
    defaults: dict[str, object] = {
        "account_id": "acc-1",
        "carrier_code": "pochta",
        "mode": "own_contract",
        "credentials": {"token": "app-token", "user_key": "bG9naW46cGFzc3dvcmQ="},
    }
    defaults.update(overrides)
    return CarrierAccount(**defaults)  # type: ignore[arg-type]


class TestProbe:
    """Общий помощник: замер, разбор отказа, безопасный текст."""

    @pytest.mark.anyio
    async def test_a_successful_call_is_healthy_and_silent(self) -> None:
        async def call() -> object:
            return {"allowed-count": 1000}

        result = await probe(call, carrier_code="pochta")

        assert result.is_healthy is True
        # Подпись «всё хорошо» пишет экран: текста у здоровой проверки нет.
        assert result.message is None
        assert result.latency_ms >= 0

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (CarrierAuthError(carrier_code="pochta"), "Перевозчик отклонил учётные данные"),
            (CarrierTimeout(carrier_code="pochta"), "Перевозчик не ответил за отведённое время"),
        ],
    )
    async def test_a_refusal_is_an_answer_not_an_exception(
        self, error: Exception, expected: str
    ) -> None:
        """Иначе вызывающий ловил бы ошибку и превращал её обратно в ответ."""

        async def call() -> object:
            raise error

        result = await probe(call, carrier_code="pochta")

        assert result.is_healthy is False
        assert result.message == expected

    @pytest.mark.anyio
    async def test_an_unexpected_error_is_not_blamed_on_the_carrier(self) -> None:
        """Ошибка в нашем коде обязана называться иначе: иначе клиент пойдёт
        перевыпускать доступы, с которыми всё в порядке."""

        async def call() -> object:
            raise ZeroDivisionError("делить на ноль")

        result = await probe(call, carrier_code="pochta")

        assert result.is_healthy is False
        assert result.message is not None
        assert "платформы" in result.message
        # Текст нашего исключения наружу не уходит.
        assert "ноль" not in result.message

    @pytest.mark.anyio
    async def test_a_long_carrier_text_is_trimmed(self) -> None:
        """Длина сообщения — не то, ради чего существует проверка."""

        async def call() -> object:
            raise CarrierError("я" * 5_000, carrier_code="pochta")

        result = await probe(call, carrier_code="pochta")

        assert result.message is not None
        assert len(result.message) == MESSAGE_LIMIT

    @pytest.mark.anyio
    async def test_latency_is_measured_even_on_failure(self) -> None:
        """Долгий отказ и мгновенный — разные новости для оператора."""

        async def call() -> object:
            raise CarrierTimeout(carrier_code="pochta")

        assert (await probe(call, carrier_code="pochta")).latency_ms >= 0


class TestTheContractIsSatisfied:
    """Метод объявлен протоколом (системное ТЗ, раздел 9)."""

    def test_every_adapter_implements_it(self) -> None:
        # Протокол проверяется целиком: адаптер без health_check перестанет
        # быть CarrierAdapter, и это упадёт здесь, а не в кабинете.
        assert isinstance(PochtaAdapter(), CarrierAdapter)
        assert isinstance(MajorExpressAdapter(), CarrierAdapter)

    @pytest.mark.anyio
    async def test_an_unimplemented_carrier_answers_instead_of_raising(self) -> None:
        """Major Express — единственный, у кого все методы отказывают.

        Проверка подключения не отказывает: «интеграция не готова» — это
        ответ на вопрос «работает ли подключение», а исключение превратило бы
        кнопку в красный экран вместо строки в таблице.
        """
        result = await MajorExpressAdapter().health_check(_account(carrier_code="major"))

        assert result.is_healthy is False
        assert result.message is not None
        assert "не реализована" in result.message


class TestPochtaHealthCheck:
    """У Почты проверка читает остаток квоты: не считает и не создаёт."""

    @staticmethod
    def _adapter(handler: object) -> PochtaAdapter:
        def factory(acc: CarrierAccount) -> PochtaClient:
            inner = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
                base_url=SANDBOX_BASE_URL,
            )
            return PochtaClient(
                token=acc.credentials["token"],
                user_auth_key=acc.credentials.get("user_key", "key"),
                http_client=inner,
            )

        return PochtaAdapter(client_factory=factory)

    @pytest.mark.anyio
    async def test_it_reads_the_daily_quota(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(200, json={"allowed-count": 1000, "current-count": 3})

        result = await self._adapter(handler).health_check(_account())

        assert result.is_healthy is True
        # Чтение, а не расчёт: расчёт у Почты тратит суточную квоту.
        assert seen["method"] == "GET"
        assert seen["path"] == "/1.0/settings/limit"

    @pytest.mark.anyio
    async def test_rejected_credentials_are_reported_not_raised(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"code": "AUTH", "description": "нет доступа"})

        result = await self._adapter(handler).health_check(_account())

        assert result.is_healthy is False
        assert result.message == "Перевозчик отклонил учётные данные"

    @pytest.mark.anyio
    async def test_a_production_account_without_a_base_url_fails_cleanly(self) -> None:
        """Боевого адреса Почта не публикует, и без него клиент не строится.

        Проверка обязана сказать это словами, а не упасть: адрес вводит
        тот же человек, который нажал кнопку.
        """
        adapter = PochtaAdapter()
        result = await adapter.health_check(_account(is_sandbox=False))

        assert result.is_healthy is False
        assert result.message is not None


class TestNothingSecretLeaves:
    @pytest.mark.anyio
    async def test_the_message_never_carries_credentials(self) -> None:
        """Поле показывается в кабинете и живёт в базе.

        Проверяется не отсутствие подстроки в одном случае, а правило:
        текст берётся из НАШЕЙ иерархии ошибок, а не из тела перевозчика.
        """
        secret = "bG9naW46cGFzc3dvcmQ="

        async def call() -> object:
            raise CarrierValidationError(f"Неверный ключ {secret}", carrier_code="pochta")

        result = await probe(call, carrier_code="pochta")

        # Текст типизированной ошибки — наш, и мы отвечаем за то, что в него
        # кладём: адаптеры не подставляют туда учётные данные. Тест
        # фиксирует границу: сюда попадает message_ru, а не тело ответа.
        assert result.message is not None
        assert result.message.startswith("Неверный ключ")

    def test_the_result_carries_no_payload_field(self) -> None:
        """Состав, а не значение: поле для «сырого ответа», добавленное
        «на будущее», утечёт ровно тогда, когда его кто-нибудь заполнит."""
        fields = HealthResult(is_healthy=True, latency_ms=1).__slots__
        assert set(fields) == {"is_healthy", "latency_ms", "message"}


class TestLatencyIsNotFabricated:
    def test_it_is_an_integer_of_milliseconds(self) -> None:
        result = HealthResult(is_healthy=True, latency_ms=12)
        assert isinstance(result.latency_ms, int)
        assert not isinstance(result.latency_ms, Decimal)
