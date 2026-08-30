"""Исходящие вебхуки тенанту: подпись, проверка адреса, отправка (FR-3.6).

Две вещи здесь важнее остального кода модуля.

**Адрес задаёт клиент, а запрос уходит с нашего сервера.** Это SSRF в чистом
виде: подписавшись на `http://169.254.169.254/…` или на адрес внутри частной
сети, клиент превратил бы платформу в инструмент разведки чужой инфраструктуры.
Поэтому адрес проверяется дважды — при подписке и **перед каждой отправкой**:
между этими моментами DNS-запись может измениться.

**Подпись покрывает время, а не только тело.** Подпись одного тела позволяет
переиграть старую доставку через год; со временем в подписи получатель может
отбросить всё, что старше своего окна.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from aerogram.shared.clock import utcnow
from aerogram.shared.errors import ValidationFailed
from aerogram.shared.logging import get_logger

__all__ = [
    "EVENT_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "WEBHOOK_EVENTS",
    "deliver",
    "generate_secret",
    "resolve",
    "sign",
    "validate_url",
]

log = get_logger(__name__)

SIGNATURE_HEADER = "X-Aerogram-Signature"
TIMESTAMP_HEADER = "X-Aerogram-Timestamp"
EVENT_HEADER = "X-Aerogram-Event"

#: События, на которые можно подписаться (FR-3.6).
WEBHOOK_EVENTS: frozenset[str] = frozenset(
    {
        "shipment.status_changed",
        "shipment.delivered",
        "shipment.exception",
        "shipment.delayed",
    }
)

#: Таймаут доставки. Клиент без явного таймаута — ошибка ревью (CLAUDE.md §6);
#: здесь он ещё и защищает воркер: медленный получатель не должен занимать
#: очередь доставок остальных тенантов.
TIMEOUT_SECONDS = 10.0

#: Ответ считается принятым при любом 2xx: получатель волен вернуть 200 или 204.
_ACCEPTED = range(200, 300)


def generate_secret() -> str:
    """Секрет подписи. Показывается клиенту один раз, как и API-ключ."""
    return secrets.token_urlsafe(32)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Подпись HMAC-SHA256 над временем и телом.

    Разделитель между ними обязателен: без него `12` + `3…` и `1` + `23…`
    дали бы одну подпись для разных доставок.
    """
    payload = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


async def validate_url(url: str) -> None:
    """Проверить, что адрес можно звать без вреда.

    Требуется HTTPS: подпись подтверждает происхождение, но не скрывает
    содержимое, а в теле — номера отправлений клиента.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValidationFailed("Адрес вебхука должен начинаться с https://", field="url")
    if not parsed.hostname:
        raise ValidationFailed("В адресе вебхука нет имени узла", field="url")
    await _ensure_public(parsed.hostname)


async def resolve(hostname: str) -> list[str]:
    """Все адреса, в которые разрешается имя.

    Отдельная функция, а не `socket.getaddrinfo` по месту, по двум причинам.

    Разрешение имени блокирующее, а вызывается оно в том числе из обработчика
    подписки: вызов по месту остановил бы весь событийный цикл на время ответа
    DNS — вместе со всеми остальными запросами процесса, включая чужих
    тенантов. Медленный или недоступный DNS получателя не должен становиться
    их проблемой, поэтому ожидание уходит в отдельный поток.

    И тесту нужно подменить разрешение имени получателя. Подменять ради этого
    атрибут самого модуля `socket` нельзя: под подмену попадает всё, что в этот
    момент открывает соединение, включая пул к базе.
    """
    infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    return [str(info[4][0]) for info in infos]


async def _ensure_public(hostname: str) -> None:
    """Отвергнуть адрес, ведущий внутрь инфраструктуры.

    Проверяются ВСЕ адреса, в которые разрешается имя: узел с одной публичной
    и одной внутренней записью иначе прошёл бы проверку и попал бы куда
    не следует.
    """
    try:
        resolved = await resolve(hostname)
    except socket.gaierror:
        raise ValidationFailed("Имя узла в адресе вебхука не разрешается", field="url") from None

    for found in resolved:
        address = ipaddress.ip_address(found)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            # В сообщение не попадает сам адрес: незачем подтверждать клиенту,
            # что именно он нащупал во внутренней сети.
            raise ValidationFailed(
                "Адрес вебхука ведёт во внутреннюю сеть и не может быть использован",
                field="url",
            )


async def deliver(url: str, secret: str, event_type: str, payload: dict[str, Any]) -> int:
    """Отправить одну доставку. Возвращает код ответа получателя.

    Адрес проверяется заново перед самой отправкой: запись DNS могла
    измениться с момента подписки. Полностью это гонку не закрывает —
    между проверкой и подключением остаётся окно, — но убирает простой
    способ подменить адрес после того, как подписку приняли.
    """
    await validate_url(url)

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(utcnow().timestamp()))
    headers = {
        "Content-Type": "application/json",
        EVENT_HEADER: event_type,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(secret, timestamp, body),
    }

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        # Перенаправления не выполняются: получатель, отвечающий 302 на чужой
        # адрес, обошёл бы проверку узла, которую мы только что сделали.
        response = await client.post(url, content=body, headers=headers)
    return response.status_code


def accepted(status: int) -> bool:
    """Принял ли получатель доставку."""
    return status in _ACCEPTED
