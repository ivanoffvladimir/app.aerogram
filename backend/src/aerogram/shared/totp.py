"""Второй фактор: TOTP по RFC 6238 поверх PyOTP (ADR-0018).

Модуль намеренно не знает ни о пользователях, ни о базе: он умеет только
породить секрет, собрать ссылку для приложения-аутентификатора и сверить код.
Хранение секрета, его шифрование и защита от повторного использования шага —
дело ядра, потому что они требуют транзакции и владельца записи.

Ключевая деталь ``verify``: она возвращает НОМЕР ШАГА, на котором код сошёлся,
а не просто «да/нет». Без номера защиту от повтора построить не на чем —
подсмотренный код действителен целых тридцать секунд, и за это время его можно
предъявить дважды.
"""

from __future__ import annotations

from datetime import datetime

import pyotp
from pyotp.utils import strings_equal

__all__ = ["INTERVAL_SECONDS", "generate_secret", "provisioning_uri", "verify"]

#: Длина шага в секундах. Значение по умолчанию RFC 6238 и всех известных
#: приложений-аутентификаторов; менять его — значит сломать уже выданные секреты.
INTERVAL_SECONDS = 30

#: Сколько соседних шагов принимается, кроме текущего. Один шаг в каждую сторону —
#: это ±30 секунд на расхождение часов телефона и сервера (ADR-0018).
_DEFAULT_WINDOW = 1


def generate_secret() -> str:
    """Новый секрет в base32 — в таком виде его понимают приложения."""
    return str(pyotp.random_base32())


def provisioning_uri(secret: str, *, account_name: str, issuer: str) -> str:
    """Ссылка ``otpauth://`` для QR-кода.

    Содержит секрет в открытом виде — это её назначение. Значит, она не логируется
    и живёт ровно один ответ на запрос подключения второго фактора.
    """
    return str(
        pyotp.TOTP(secret, interval=INTERVAL_SECONDS).provisioning_uri(
            name=account_name, issuer_name=issuer
        )
    )


def verify(secret: str, code: str, *, at: datetime, window: int = _DEFAULT_WINDOW) -> int | None:
    """Сверить код и вернуть номер сошедшегося шага, либо ``None``.

    Сравнение постоянного времени: посимвольное сравнение шестизначного кода
    измеримо утекает, сколько первых цифр угаданы.

    Шаги перебираются от текущего к дальним, чтобы вернуть ближайший
    подошедший — на нём и строится отсечка повторов.
    """
    digits = code.strip()
    if not digits.isdigit():
        return None
    totp = pyotp.TOTP(secret, interval=INTERVAL_SECONDS)
    current = totp.timecode(at)
    matched: int | None = None
    for offset in _offsets(window):
        step = current + offset
        # Проверяются все шаги окна, без раннего выхода: время ответа не должно
        # зависеть от того, на каком шаге код сошёлся.
        if strings_equal(totp.generate_otp(step), digits) and matched is None:
            matched = step
    return matched


def _offsets(window: int) -> tuple[int, ...]:
    """Смещения шагов окна, от ближних к дальним: 0, -1, +1, -2, +2, …"""
    offsets = [0]
    for distance in range(1, window + 1):
        offsets.extend((-distance, distance))
    return tuple(offsets)
