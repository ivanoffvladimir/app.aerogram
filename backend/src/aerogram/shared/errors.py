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


class CarrierNotConfigured(CarrierError):
    """Интеграция объявлена, но не готова к работе.

    Отдельно от ``CarrierUnavailable`` намеренно: «недоступен» означает
    «подождите и повторите», а здесь ждать бесполезно — нужны действия
    администратора. Повторять такой отказ не следует, поэтому его кода нет
    в наборе повторяемых.
    """

    code = "carrier_not_configured"
    message_ru = "Интеграция с перевозчиком не настроена"


class CarrierUnavailable(CarrierError):
    code = "carrier_unavailable"
    message_ru = "Перевозчик временно недоступен"


class CarrierRateLimited(CarrierError):
    code = "carrier_rate_limited"
    message_ru = "Превышен лимит обращений к перевозчику"


class DirectoryError(AerogramError):
    """Ошибка внешнего справочника (ДаData).

    502, а не 500, по той же причине, что и у ``CarrierError``: отвечает плохо
    внешняя система, а не наша. Отдельное семейство от ``CarrierError`` нужно
    потому, что в формате FR-10.5 у ошибок перевозчика заполняется
    ``carrier_code`` и текст говорит «Перевозчик вернул ошибку» — для человека,
    вводящего адрес, это была бы ложь.
    """

    code = "directory_error"
    http_status = 502
    message_ru = "Справочник адресов вернул ошибку"


class DirectoryUnavailable(DirectoryError):
    code = "directory_unavailable"
    message_ru = "Справочник адресов временно недоступен"


class DirectoryAuthError(DirectoryError):
    code = "directory_auth_error"
    message_ru = "Справочник адресов отклонил учётные данные"


class DirectoryQuotaExceeded(DirectoryError):
    """Исчерпан суточный лимит обращений к справочнику.

    Лимит бесплатного тарифа ДаData — 10 000 запросов в сутки, и каждый
    введённый символ в подсказке тратит один. Исчерпание в середине рабочего
    дня — штатный сценарий, а не авария, поэтому у него отдельный код.
    """

    code = "directory_quota_exceeded"
    message_ru = "Исчерпан суточный лимит обращений к справочнику адресов"


class AddressNotResolved(ValidationFailed):
    """Не удалось определить населённый пункт по адресу."""

    code = "address_not_resolved"
    message_ru = "Не удалось определить населённый пункт. Уточните адрес"


class CityNotMapped(ValidationFailed):
    """У перевозчика нет кода для этого населённого пункта (FR-8.2).

    На пути расчёта это не исключение, а отдельная строка выдачи: ошибка одного
    перевозчика не роняет выдачу (FR-1.4). Исключение поднимается только на пути
    создания отправления, где продолжать нельзя.
    """

    code = "city_not_mapped"
    message_ru = "Перевозчик не обслуживает этот населённый пункт"
