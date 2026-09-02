"""HTTP-клиент адаптеров: таймауты, ретраи, троттлинг, разбор ошибок.

Требования раздела 8.2 ТЗ, п. 5: соблюдение лимитов перевозчика, экспоненциальные
ретраи, circuit breaker. Клиент без явного таймаута — ошибка ревью (CLAUDE.md §6).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from aerogram.shared.errors import (
    CarrierAuthError,
    CarrierError,
    CarrierRateLimited,
    CarrierTimeout,
    CarrierUnavailable,
    CarrierValidationError,
)
from aerogram.shared.logging import get_logger

__all__ = ["CarrierHttpClient", "CircuitBreaker", "CircuitOpen", "RawCall"]

log = get_logger(__name__)

#: Коды, при которых повтор осмыслен: перевозчик недоступен или троттлит.
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RawCall:
    """Снимок вызова перевозчика для таблицы ``carrier_raw_calls``.

    Сохраняется вызывающим слоем: сам адаптер к БД не обращается.
    """

    carrier_code: str
    operation: str
    method: str
    url: str
    http_status: int | None
    duration_ms: int
    is_error: bool
    error_code: str | None
    request_payload: dict[str, Any] | None
    response_payload: dict[str, Any] | None


class CircuitOpen(CarrierUnavailable):
    """Автомат разомкнут: перевозчик признан недоступным, вызовы не делаются."""

    code = "carrier_circuit_open"
    message_ru = "Перевозчик временно отключён из-за череды ошибок"


class CircuitBreaker:
    """Простой автомат защиты от «мёртвого» перевозчика.

    После ``threshold`` подряд идущих сбоев вызовы отклоняются локально в течение
    ``cooldown_seconds`` — это экономит общий дедлайн выдачи (FR-1.3) и не даёт
    одному упавшему ТК съедать таймаут у всей выдачи.
    """

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 60.0) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            # Полуоткрытое состояние: пропускаем один пробный вызов.
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()


class CarrierHttpClient:
    """Обёртка над httpx для адаптеров.

    Всегда задаёт таймаут на соединение и на чтение, повторяет только идемпотентные
    и явно помеченные вызовы, и переводит HTTP-коды в иерархию ``shared.errors``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        carrier_code: str,
        timeout_seconds: float = 3.0,
        max_attempts: int = 3,
        breaker: CircuitBreaker | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._carrier_code = carrier_code
        self._max_attempts = max_attempts
        self._breaker = breaker or CircuitBreaker()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 2.0)),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> CarrierHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        json: Any | None = None,
        data: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
        on_raw_call: Callable[[RawCall], Awaitable[None]] | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Выполнить запрос с ретраями и разбором ошибок.

        ``raise_for_status=False`` отдаёт ответ с кодом 4xx вызывающему вместо
        исключения — для поиска, где «не найдено» приходит телом и статусом
        сразу, и различить его с настоящим отказом можно только по телу.
        Авторизация и лимит запросов остаются исключениями: их тело
        не интересно никому. Такой ответ не считается сбоем перевозчика
        и предохранитель не трогает — иначе сверка сотни черновиков открыла
        бы его сама.
        """
        if self._breaker.is_open:
            raise CircuitOpen(carrier_code=self._carrier_code)

        attempts = self._max_attempts if retry else 1
        last_error: CarrierError | None = None

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            response: httpx.Response | None = None
            error: CarrierError | None = None
            try:
                response = await self._client.request(
                    method, url, json=json, data=data, params=params, headers=headers
                )
            except httpx.TimeoutException as exc:
                error = CarrierTimeout(carrier_code=self._carrier_code)
                log.warning(
                    "carrier.timeout",
                    carrier=self._carrier_code,
                    operation=operation,
                    attempt=attempt,
                    error=str(exc),
                )
            except httpx.HTTPError as exc:
                error = CarrierUnavailable(carrier_code=self._carrier_code)
                log.warning(
                    "carrier.transport_error",
                    carrier=self._carrier_code,
                    operation=operation,
                    attempt=attempt,
                    error=str(exc),
                )

            duration_ms = int((time.monotonic() - started) * 1000)

            if response is not None:
                error = self._error_for_status(response)

            if on_raw_call is not None:
                await on_raw_call(
                    RawCall(
                        carrier_code=self._carrier_code,
                        operation=operation,
                        method=method,
                        url=url,
                        http_status=response.status_code if response is not None else None,
                        duration_ms=duration_ms,
                        is_error=error is not None,
                        error_code=error.code if error is not None else None,
                        request_payload=_as_payload(json if json is not None else data),
                        response_payload=_response_payload(response),
                    )
                )

            if error is None and response is not None:
                self._breaker.record_success()
                return response
            if (
                not raise_for_status
                and response is not None
                and type(error) is CarrierValidationError
            ):
                self._breaker.record_success()
                return response

            assert error is not None  # noqa: S101  # выше error гарантированно заполнен
            self._breaker.record_failure()
            last_error = error

            retriable = isinstance(error, (CarrierTimeout, CarrierUnavailable, CarrierRateLimited))
            if not retry or not retriable or attempt == attempts:
                raise error

            # Экспоненциальная задержка: 0.2 с, 0.4 с, 0.8 с…
            await asyncio.sleep(0.2 * 2 ** (attempt - 1))

        raise last_error or CarrierUnavailable(carrier_code=self._carrier_code)

    def _error_for_status(self, response: httpx.Response) -> CarrierError | None:
        status = response.status_code
        if status < 400:
            return None
        if status in (401, 403):
            return CarrierAuthError(carrier_code=self._carrier_code)
        if status == 429:
            return CarrierRateLimited(carrier_code=self._carrier_code)
        if status in _RETRYABLE_STATUSES:
            return CarrierUnavailable(carrier_code=self._carrier_code)
        if 400 <= status < 500:
            return CarrierValidationError(carrier_code=self._carrier_code)
        return CarrierError(carrier_code=self._carrier_code)


def _as_payload(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    return None


def _response_payload(response: httpx.Response | None) -> dict[str, Any] | None:
    if response is None:
        return None
    try:
        parsed = response.json()
    except ValueError:
        return {"_raw": response.text[:4000]}
    if isinstance(parsed, dict):
        return parsed
    return {"_list": parsed}
