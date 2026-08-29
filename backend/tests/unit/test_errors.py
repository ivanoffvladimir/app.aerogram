"""Иерархия ошибок и единый формат ответа (FR-10.5, раздел 8.2 ТЗ)."""

from __future__ import annotations

import pytest

from aerogram.shared.errors import (
    AerogramError,
    CarrierAuthError,
    CarrierError,
    CarrierTimeout,
    CarrierValidationError,
    NotFound,
    TenantIsolationError,
)


class TestPayloadFormat:
    def test_matches_specified_shape(self) -> None:
        payload = CarrierTimeout(carrier_code="cdek").as_payload("rq_123")
        assert set(payload["error"]) == {  # type: ignore[arg-type]
            "code",
            "message",
            "field",
            "carrier_code",
            "request_id",
        }

    def test_message_is_russian(self) -> None:
        payload = CarrierTimeout(carrier_code="cdek").as_payload("rq_123")
        assert payload["error"]["message"] == "Перевозчик не ответил за отведённое время"  # type: ignore[index]

    def test_carries_request_id(self) -> None:
        payload = NotFound().as_payload("rq_abc")
        assert payload["error"]["request_id"] == "rq_abc"  # type: ignore[index]

    def test_custom_message_overrides_default(self) -> None:
        assert NotFound("Отправление не найдено").message_ru == "Отправление не найдено"


class TestCarrierErrorsNeverBecome500:
    """Ошибка перевозчика никогда не приводит к 500 (раздел 8.2 ТЗ).

    502 — «внешняя система ответила плохо», это честно и не поднимает тревогу
    как отказ самой платформы.
    """

    @pytest.mark.parametrize(
        "error",
        [CarrierError(), CarrierTimeout(), CarrierAuthError(), CarrierValidationError()],
    )
    def test_status_is_not_500(self, error: CarrierError) -> None:
        assert error.http_status != 500
        assert error.http_status == 502


class TestTenantIsolation:
    def test_returns_404_not_403(self) -> None:
        """Раздел 14.1 ТЗ: доступ к чужому объекту — 404, а не 403.

        403 подтвердил бы существование объекта и превратил бы идентификаторы
        в канал утечки.
        """
        assert TenantIsolationError().http_status == 404


def test_all_errors_have_russian_message() -> None:
    def descendants(cls: type[AerogramError]) -> list[type[AerogramError]]:
        result: list[type[AerogramError]] = []
        for sub in cls.__subclasses__():
            result.append(sub)
            result.extend(descendants(sub))
        return result

    for error_class in descendants(AerogramError):
        assert error_class.message_ru, f"{error_class.__name__} без русского сообщения"
        assert error_class.code != "internal_error", f"{error_class.__name__} без своего кода"
