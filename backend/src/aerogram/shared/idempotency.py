"""Идемпотентность запросов, изменяющих состояние.

Контракт требует ``Idempotency-Key`` на ``POST /v1/decisions`` и
``POST /v1/shipments`` (бэкенд-ТЗ, раздел 6). Правило простое и жёсткое:

* тот же ключ и то же тело — тот же результат, без повторного действия;
* тот же ключ и ДРУГОЕ тело — ``409``.

Второе важнее первого. Без него повтор с изменённым телом молча создал бы
второе отправление у перевозчика, а клиент увидел бы ответ от первого.

Отпечаток тела считается по каноническому JSON: порядок ключей не должен
превращать тот же запрос в другой.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import Header

from aerogram.shared.errors import Conflict

__all__ = ["IdempotencyKey", "ensure_same_request", "request_fingerprint"]

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        description="Ключ идемпотентности запроса. Повтор с тем же ключом "
        "и тем же телом возвращает тот же результат.",
    ),
]


def request_fingerprint(payload: Any) -> str:
    """Отпечаток тела запроса: канонический JSON, затем SHA-256."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_same_request(stored_fingerprint: str, payload: Any) -> None:
    """Убедиться, что повтор пришёл с тем же телом.

    Расхождение — ``409``, а не «вернём прошлый ответ»: клиент, изменивший
    тело, ждёт нового действия, и молча отдать ему чужой результат хуже,
    чем честно отказать.
    """
    if stored_fingerprint != request_fingerprint(payload):
        raise Conflict(
            "Этот ключ идемпотентности уже использован с другим содержимым запроса",
            field="Idempotency-Key",
        )
