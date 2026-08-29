"""HTTP-клиент адаптеров: таймауты, ретраи, разбор ошибок, circuit breaker.

Раздел 8.2 ТЗ, п. 5. Сеть не используется: транспорт подменяется на управляемый,
поэтому тесты детерминированы и идут в CI без доступа наружу.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from aerogram.carriers.http import CarrierHttpClient, CircuitBreaker, CircuitOpen, RawCall
from aerogram.shared.errors import (
    CarrierAuthError,
    CarrierRateLimited,
    CarrierTimeout,
    CarrierUnavailable,
    CarrierValidationError,
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
) -> CarrierHttpClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(transport=transport, base_url="https://api.example.com")
    return CarrierHttpClient(
        base_url="https://api.example.com",
        carrier_code="cdek",
        client=inner,
        **kwargs,  # type: ignore[arg-type]
    )


class TestSuccessfulCalls:
    async def test_returns_response(self) -> None:
        client = _client(lambda _: httpx.Response(200, json={"ok": True}))
        response = await client.request("GET", "/ping", operation="quote")
        assert response.json() == {"ok": True}
        await client.aclose()

    async def test_records_raw_call_for_forensics(self) -> None:
        """Сырьё вызова уходит вызывающему слою (раздел 8.2 ТЗ, п. 6)."""
        captured: list[RawCall] = []

        async def capture(call: RawCall) -> None:
            captured.append(call)

        client = _client(lambda _: httpx.Response(200, json={"tariffs": []}))
        await client.request("POST", "/calc", operation="quote", json={"a": 1}, on_raw_call=capture)
        await client.aclose()

        assert len(captured) == 1
        assert captured[0].carrier_code == "cdek"
        assert captured[0].operation == "quote"
        assert captured[0].http_status == 200
        assert captured[0].is_error is False
        assert captured[0].request_payload == {"a": 1}


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, CarrierAuthError),
            (403, CarrierAuthError),
            (400, CarrierValidationError),
            (422, CarrierValidationError),
        ],
    )
    async def test_client_errors_map_without_retry(
        self, status: int, expected: type[Exception]
    ) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status, json={"error": "нет"})

        client = _client(handler)
        with pytest.raises(expected):
            await client.request("POST", "/orders", operation="create")
        await client.aclose()
        # Повторять заведомо отвергнутый запрос бессмысленно и вредно:
        # у создания заказа это риск дубля.
        assert calls == 1

    async def test_timeout_becomes_carrier_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("слишком долго", request=request)

        client = _client(handler, max_attempts=1)
        with pytest.raises(CarrierTimeout):
            await client.request("POST", "/calc", operation="quote")
        await client.aclose()

    async def test_no_carrier_error_is_http_500(self) -> None:
        """Ошибка перевозчика никогда не превращается в 500 (раздел 8.2 ТЗ)."""
        client = _client(lambda _: httpx.Response(500))
        with pytest.raises(Exception) as info:
            await client.request("GET", "/x", operation="track", retry=False)
        await client.aclose()
        assert info.value.http_status == 502  # type: ignore[attr-defined]


class TestRetries:
    async def test_retries_transient_failure_then_succeeds(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        client = _client(handler, max_attempts=3)
        response = await client.request("GET", "/calc", operation="quote")
        await client.aclose()
        assert response.status_code == 200
        assert attempts == 3

    async def test_gives_up_after_max_attempts(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        client = _client(handler, max_attempts=2)
        with pytest.raises(CarrierUnavailable):
            await client.request("GET", "/calc", operation="quote")
        await client.aclose()
        assert attempts == 2

    async def test_rate_limit_is_retried(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(429)

        client = _client(handler, max_attempts=2)
        with pytest.raises(CarrierRateLimited):
            await client.request("GET", "/calc", operation="quote")
        await client.aclose()
        assert attempts == 2

    async def test_retry_can_be_disabled_for_non_idempotent_calls(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        client = _client(handler, max_attempts=3)
        with pytest.raises(CarrierUnavailable):
            await client.request("POST", "/orders", operation="create", retry=False)
        await client.aclose()
        assert attempts == 1


class TestCircuitBreaker:
    def test_opens_after_threshold(self) -> None:
        breaker = CircuitBreaker(threshold=3, cooldown_seconds=60)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.is_open is True

    def test_success_resets_failures(self) -> None:
        breaker = CircuitBreaker(threshold=3, cooldown_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.is_open is False

    def test_closes_after_cooldown(self) -> None:
        breaker = CircuitBreaker(threshold=1, cooldown_seconds=0)
        breaker.record_failure()
        assert breaker.is_open is False

    async def test_open_circuit_rejects_without_calling_carrier(self) -> None:
        """Смысл автомата: не тратить общий дедлайн выдачи на мёртвого перевозчика."""
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
        client = _client(handler, max_attempts=1, breaker=breaker)

        with pytest.raises(CarrierUnavailable):
            await client.request("GET", "/calc", operation="quote")
        assert calls == 1

        with pytest.raises(CircuitOpen):
            await client.request("GET", "/calc", operation="quote")
        await client.aclose()
        assert calls == 1, "при разомкнутом автомате запрос к перевозчику не уходит"
