"""Объектное хранилище документов (ADR-0016).

**Почему `boto3`, а не асинхронный клиент.** Дорогих обращений к хранилищу
здесь ровно одно — запись файла, и происходит она в воркере, где занятый
поток безобиден. Остальное — выбор ключа и отдача байтов кабинету, причём
байты идут через наш API, а не подписанной ссылкой: в этикетке ФИО, адрес
и телефон получателя, и ссылка-предъявитель на персональные данные жила бы
сутки у всякого, кому её переслали. Одна зависимость вместо трёх и самый
проверенный клиент перевесили полную асинхронность.

**Блокирующие вызовы уходят в поток.** `boto3` синхронный, а событийный цикл
обслуживает все запросы процесса, включая чужих тенантов: вызов по месту
остановил бы их всех на время ответа хранилища.

**Изоляции тенантов в S3 нет.** Её здесь и не может быть: разграничение
держит RLS на таблице `documents`, а сюда ключ попадает уже из строки,
которую тенанту отдала база. Тенант всё равно стоит в самом ключе — чтобы
разбор в консоли хранилища и ручная уборка были возможны без обращения
к базе.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aerogram.config import Settings
from aerogram.shared.enums import DocumentFormat
from aerogram.shared.errors import StorageUnavailable
from aerogram.shared.logging import get_logger

__all__ = [
    "CONTENT_TYPES",
    "ObjectStorage",
    "document_key",
]

log = get_logger(__name__)

#: Результат операции хранилища. Параметр типа, а не ``Any``: иначе
#: нетипизированный ``boto3`` растёк бы отсюда по всему модулю.
_T = TypeVar("_T")

#: Тип содержимого по формату документа. Нужен и хранилищу, и браузеру:
#: PDF с `application/octet-stream` скачивается вместо того, чтобы открыться.
CONTENT_TYPES: dict[DocumentFormat, str] = {
    DocumentFormat.PDF: "application/pdf",
    DocumentFormat.PNG: "image/png",
    # ZPL — язык принтера, а не документ для человека: свой тип у него есть,
    # но браузеру он ничего не говорит, и файл всегда скачивается.
    DocumentFormat.ZPL: "application/vnd.zebra.zpl",
}


def document_key(tenant_id: UUID, shipment_id: UUID, document_id: UUID, fmt: DocumentFormat) -> str:
    """Ключ объекта. Тенант первым — чтобы префикс отделял клиентов друг
    от друга и в консоли хранилища, и в правилах жизненного цикла."""
    return f"tenants/{tenant_id}/shipments/{shipment_id}/{document_id}.{fmt.value}"


class ObjectStorage:
    """Файлы документов. Ни одной проверки прав: их делает вызывающий."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bucket = settings.s3_bucket

    def _client(self) -> Any:
        """Клиент S3. Создаётся на вызов: `boto3` не потокобезопасен, а мы
        уходим в поток на каждой операции."""
        if not (self._settings.s3_access_key and self._settings.s3_secret_key):
            # Молчаливое падение внутри boto3 выглядело бы как сбой сети.
            raise StorageUnavailable("Хранилище документов не настроено")
        return boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint_url,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            region_name=self._settings.s3_region,
        )

    async def put(self, key: str, body: bytes, *, content_type: str) -> None:
        """Положить файл. Перезапись существующего ключа — норма: ключ
        выведен из идентификатора документа, то есть уникален по построению."""
        await self._run("put", key, self._put, key, body, content_type)

    async def get(self, key: str) -> bytes:
        """Забрать файл целиком.

        Целиком, а не потоком: этикетка — это десятки килобайт, и потоковая
        отдача усложнила бы код ради экономии, которой нет.
        """
        return await self._run("get", key, self._get, key)

    async def delete(self, key: str) -> None:
        await self._run("delete", key, self._delete, key)

    # --- Синхронная часть: выполняется в отдельном потоке ------------------

    def _put(self, key: str, body: bytes, content_type: str) -> None:
        self._client().put_object(Bucket=self._bucket, Key=key, Body=body, ContentType=content_type)

    def _get(self, key: str) -> bytes:
        response = self._client().get_object(Bucket=self._bucket, Key=key)
        # ``bytes()`` не для красоты: клиент нетипизирован, и без приведения
        # ``Any`` разошёлся бы отсюда по всему модулю (CLAUDE.md §6).
        return bytes(response["Body"].read())

    def _delete(self, key: str) -> None:
        self._client().delete_object(Bucket=self._bucket, Key=key)

    async def _run(self, action: str, key: str, func: Callable[..., _T], *args: object) -> _T:
        """Выполнить операцию в потоке и превратить отказ хранилища в свою ошибку.

        Ключ в лог не попадает целиком: в нём идентификатор тенанта, а этого
        достаточно, чтобы по журналу восстановить, кто чем пользуется.
        Достаточно знать, какая операция и по какому документу не удалась.
        """
        try:
            return await asyncio.to_thread(func, *args)
        except (BotoCoreError, ClientError) as error:
            log.error(
                "documents.storage_failed",
                action=action,
                document=key.rsplit("/", 1)[-1],
                error_type=type(error).__name__,
            )
            raise StorageUnavailable() from error
