"""UUIDv7 — первичные ключи, сортируемые по времени.

Причина выбора (ADR-0003): монотонность по времени даёт локальность в B-tree индексах
и не раскрывает клиентам порядковые номера, как это делает bigserial.
"""

from __future__ import annotations

import os
import time
from uuid import UUID

__all__ = ["uuid7", "uuid7_timestamp"]

_VARIANT_RFC4122 = 0b10


def uuid7() -> UUID:
    """Сгенерировать UUID версии 7 (RFC 9562).

    Раскладка: 48 бит — unix-время в миллисекундах, 4 бита версии, 12 бит случайных,
    2 бита варианта, 62 бита случайных.
    """
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")

    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)

    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= _VARIANT_RFC4122 << 62
    value |= rand_b
    return UUID(int=value)


def uuid7_timestamp(value: UUID) -> int:
    """Извлечь unix-время в миллисекундах из UUIDv7.

    Полезно в отладке и в разборе инцидентов: по идентификатору строки видно,
    когда она создана, без обращения к created_at.
    """
    if value.version != 7:
        raise ValueError(f"ожидался UUIDv7, получен UUIDv{value.version}")
    return value.int >> 80
