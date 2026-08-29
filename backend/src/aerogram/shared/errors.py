"""Единая иерархия ошибок и единый формат ответа API (FR-10.5).

Правило, которое не обсуждается: ошибка перевозчика **никогда** не превращается в 500.
Она становится строкой выдачи с кодом и русскоязычным текстом (FR-1.4).
"""

from __future__ import annotations

__all__ = [
    "AerogramError",
    "AuthenticationError",
    "CarrierAuthError",
    "CarrierError",
    "CarrierRateLimited",
    "CarrierTimeout",
    "CarrierUnavailable",
    "CarrierValidationError",
    "Conflict",
    "NotFound",
    "PermissionDenied",
    "RateLimited",
    "TenantIsolationError",
    "ValidationFailed",
]


class AerogramError(Exception):
    """Базовая ошибка домена.

    ``code`` — машинный код для клиента, ``message_ru`` — текст для человека.
    """

    code: str = "internal_error"
    http_status: int = 400
    message_ru: str = "Внутренняя ошибка"

    def __init__(
        self,
        message_ru: str | None = None,
        *,
        field: str | None = None,
        carrier_code: str | None = None,
    ) -> None:
        self.message_ru = message_ru or type(self).message_ru
        self.field = field
        self.carrier_code = carrier_code
        super().__init__(self.message_ru)

    def as_payload(self, request_id: str) -> dict[str, object]:
        """Тело ответа API в формате FR-10.5."""
        return {
            "error": {
                "code": self.code,
                "message": self.message_ru,
                "field": self.field,
                "carrier_code": self.carrier_code,
                "request_id": request_id,
            }
        }


class ValidationFailed(AerogramError):
    code = "validation_failed"
    http_status = 422
    message_ru = "Данные не прошли проверку"


class NotFound(AerogramError):
    code = "not_found"
    http_status = 404
    message_ru = "Объект не найден"


class Conflict(AerogramError):
    code = "conflict"
    http_status = 409
    message_ru = "Конфликт состояния"


class AuthenticationError(AerogramError):
    code = "unauthenticated"
    http_status = 401
    message_ru = "Требуется авторизация"


class PermissionDenied(AerogramError):
    code = "permission_denied"
    http_status = 403
    message_ru = "Недостаточно прав"


class RateLimited(AerogramError):
    code = "rate_limited"
    http_status = 429
    message_ru = "Слишком много запросов, попробуйте позже"


class TenantIsolationError(AerogramError):
    """Попытка работы без установленного тенанта.

    Считается программной ошибкой, а не пользовательской: наружу отдаётся 404,
    чтобы не подтверждать существование чужого объекта (раздел 7.2 ТЗ).
    """

    code = "tenant_isolation"
    http_status = 404
    message_ru = "Объект не найден"


class CarrierError(AerogramError):
    """Ошибка на стороне перевозчика. Не приводит к 500 (раздел 8.2 ТЗ)."""

    code = "carrier_error"
    http_status = 502
    message_ru = "Перевозчик вернул ошибку"


class CarrierTimeout(CarrierError):
    code = "carrier_timeout"
    message_ru = "Перевозчик не ответил за отведённое время"


class CarrierAuthError(CarrierError):
    code = "carrier_auth_error"
    message_ru = "Перевозчик отклонил учётные данные"


class CarrierValidationError(CarrierError):
    code = "carrier_validation_error"
    message_ru = "Перевозчик отклонил данные отправления"


class CarrierUnavailable(CarrierError):
    code = "carrier_unavailable"
    message_ru = "Перевозчик временно недоступен"


class CarrierRateLimited(CarrierError):
    code = "carrier_rate_limited"
    message_ru = "Превышен лимит обращений к перевозчику"
