"""Исходящие вебхуки тенанту: подпись, проверка адреса, отправка (FR-3.6).

Две вещи здесь важнее остального кода модуля.

**Адрес задаёт клиент, а запрос уходит с нашего сервера.** Это SSRF в чистом
виде: подписавшись на `http://169.254.169.254/…` или на адрес внутри частной
сети, клиент превратил бы платформу в инструмент разведки чужой инфраструктуры.
Поэтому адрес проверяется при подписке и **заново перед каждой отправкой**,
а соединение открывается **по проверенному адресу, а не по имени**.

Последнее и есть разница между «проверили» и «подключились туда, что
проверили». Проверка имени с последующим подключением по имени оставляет
окно: имя разрешается ДВАЖДЫ — нами при проверке и клиентом HTTP при
подключении, — и во второй раз оно может разрешиться уже в `169.254.169.254`.
Это классическая подмена DNS (DNS rebinding), и от неё не спасает ни короткий
TTL, ни повторная проверка: она не про время, а про два разных разрешения
одного имени. Закрывается только тем, что подключаемся по адресу, который
проверили сами.

Имя узла при этом никуда не девается: оно остаётся в заголовке `Host`
и в SNI, поэтому сертификат получателя проверяется против ИМЕНИ, а не против
адреса. Подключение по IP без этого сломало бы TLS у любого нормального
получателя — и починка «отключить проверку сертификата» была бы лекарством
хуже болезни.

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
    "http_client",
    "pin_to_address",
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


async def validate_url(url: str) -> list[str]:
    """Проверить, что адрес можно звать без вреда, и вернуть проверенные адреса.

    Требуется HTTPS: подпись подтверждает происхождение, но не скрывает
    содержимое, а в теле — номера отправлений клиента.

    Адреса возвращаются, а не выбрасываются, потому что подключаться нужно
    именно к ним: разрешить имя ещё раз при подключении значит вернуть окно,
    которое эта проверка и закрывает.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValidationFailed("Адрес вебхука должен начинаться с https://", field="url")
    if not parsed.hostname:
        raise ValidationFailed("В адресе вебхука нет имени узла", field="url")
    return await _ensure_public(parsed.hostname)


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


async def _ensure_public(hostname: str) -> list[str]:
    """Отвергнуть адрес, ведущий внутрь инфраструктуры, и вернуть остальные.

    Проверяются ВСЕ адреса, в которые разрешается имя, и достаточно одного
    внутреннего, чтобы отказать целиком: узел с одной публичной и одной
    внутренней записью иначе прошёл бы проверку, а подключение досталось бы
    той записи, которую выберет система.
    """
    try:
        resolved = await resolve(hostname)
    except socket.gaierror:
        raise ValidationFailed("Имя узла в адресе вебхука не разрешается", field="url") from None

    if not resolved:
        # Пустой ответ резолвера — не «всё хорошо»: подключаться некуда,
        # и молча пропустить его значило бы вернуться к подключению по имени.
        raise ValidationFailed("Имя узла в адресе вебхука не разрешается", field="url")

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
    return resolved


def pin_to_address(url: str, address: str) -> tuple[httpx.URL, str, str]:
    """Куда подключаться, что писать в ``Host`` и какое имя предъявлять в SNI.

    Возвращает тройку, а не один адрес, потому что подключение по IP меняет
    сразу три вещи, и забыть любую из них — сломать либо безопасность, либо
    самого получателя:

    * **адрес соединения** — проверенный IP, чтобы имя не разрешалось второй
      раз;
    * **``Host``** — прежнее имя с портом: по нему получатель выбирает
      виртуальный узел, и IP в этом заголовке привёл бы к 404 у любого
      хостинга с несколькими сайтами на адресе;
    * **SNI** — прежнее имя: по нему получатель выбирает сертификат, и по нему
      же мы этот сертификат проверяем. Без SNI проверка шла бы против IP
      и падала бы на любом нормальном сертификате.

    IPv6-адрес в URL берётся в скобки — это делает сам ``httpx``.
    """
    original = httpx.URL(url)
    return original.copy_with(host=address), original.netloc.decode("ascii"), original.host


def http_client() -> httpx.AsyncClient:
    """Клиент доставки. Отдельная функция по двум причинам.

    Политика соединения — таймаут и запрет перенаправлений — задаётся в одном
    месте, а не повторяется у каждого вызова. И тесту нужно подменить
    транспорт: доставка обязана проверяться без настоящей сети, иначе
    проверять её будут только в бою.
    """
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)


async def deliver(url: str, secret: str, event_type: str, payload: dict[str, Any]) -> int:
    """Отправить одну доставку. Возвращает код ответа получателя.

    Адрес проверяется заново перед самой отправкой — запись DNS могла
    измениться с момента подписки, — и подключение идёт **к проверенному
    адресу**. Второго разрешения имени не происходит, поэтому подменить его
    между проверкой и подключением больше нечем.
    """
    addresses = await validate_url(url)
    # Берём первый: проверку прошли ВСЕ, отказ был бы общим на весь узел,
    # — значит любой из них безопасен, и выбирать между ними не по чему.
    target, host, sni = pin_to_address(url, addresses[0])

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(utcnow().timestamp()))
    headers = {
        "Content-Type": "application/json",
        "Host": host,
        EVENT_HEADER: event_type,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(secret, timestamp, body),
    }

    async with http_client() as client:
        # Перенаправления не выполняются: получатель, отвечающий 302 на чужой
        # адрес, обошёл бы и проверку узла, и подключение по проверенному
        # адресу — httpx пошёл бы по новому URL уже по имени.
        response = await client.post(
            target, content=body, headers=headers, extensions={"sni_hostname": sni}
        )
    return response.status_code


def accepted(status: int) -> bool:
    """Принял ли получатель доставку."""
    return status in _ACCEPTED
