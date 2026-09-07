"""Объектное хранилище документов: ключ, тип содержимого, отказы.

Настоящего S3 здесь нет — подменяется синхронная часть, которая зовёт
`boto3`. Под проверкой остаётся всё наше: построение ключа, перевод отказа
хранилища в доменную ошибку и то, что ключ не утекает в журнал целиком.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from aerogram.config import Settings
from aerogram.documents.storage import CONTENT_TYPES, ObjectStorage, document_key
from aerogram.shared.enums import DocumentFormat
from aerogram.shared.errors import StorageUnavailable
from aerogram.shared.ids import uuid7

TENANT = uuid7()
SHIPMENT = uuid7()
DOCUMENT = uuid7()


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret": "x" * 40,
        "credential_keys": "k1:" + "A" * 43 + "=",
        "s3_access_key": "key",
        "s3_secret_key": "secret",
    }
    return Settings(**{**base, **overrides})


class TestKey:
    def test_the_tenant_comes_first(self) -> None:
        """Префикс отделяет клиентов друг от друга и в консоли хранилища,
        и в правилах жизненного цикла."""
        key = document_key(TENANT, SHIPMENT, DOCUMENT, DocumentFormat.PDF)
        assert key.startswith(f"tenants/{TENANT}/")

    def test_the_key_is_unique_by_document(self) -> None:
        """Ключ выведен из идентификатора документа, поэтому перезапись
        своего же файла — норма, а чужого не случается."""
        first = document_key(TENANT, SHIPMENT, uuid7(), DocumentFormat.PDF)
        second = document_key(TENANT, SHIPMENT, uuid7(), DocumentFormat.PDF)
        assert first != second

    def test_the_extension_matches_the_format(self) -> None:
        assert document_key(TENANT, SHIPMENT, DOCUMENT, DocumentFormat.ZPL).endswith(".zpl")


class TestContentTypes:
    def test_every_format_has_one(self) -> None:
        """Без типа PDF скачивается вместо того, чтобы открыться."""
        assert set(CONTENT_TYPES) == set(DocumentFormat)


@pytest.mark.asyncio
class TestRefusals:
    async def test_an_unconfigured_storage_says_so(self) -> None:
        """Иначе отказ выглядел бы сбоем сети, и его искали бы не там."""
        storage = ObjectStorage(settings(s3_access_key=None, s3_secret_key=None))
        with pytest.raises(StorageUnavailable):
            await storage.put("k", b"x", content_type="application/pdf")

    async def test_a_refusal_of_the_store_becomes_our_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Клиенту нужен ``503`` и «попробуйте позже», а не трассировка boto3."""

        def boom(*_: object) -> None:
            raise ClientError({"Error": {"Code": "NoSuchBucket"}}, "GetObject")

        storage = ObjectStorage(settings())
        monkeypatch.setattr(storage, "_get", boom)
        with pytest.raises(StorageUnavailable):
            await storage.get("tenants/x/shipments/y/z.pdf")

    async def test_the_key_does_not_reach_the_log_whole(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """В ключе идентификатор тенанта: по журналу иначе видно, кто чем
        пользуется, а этого журналу знать незачем.

        Журнал читается со стандартного вывода, а не через ``caplog``:
        structlog пишет мимо стандартного logging, и ``caplog`` увидел бы
        пустоту — то есть тест зеленел бы и на утечке.
        """

        def boom(*_: object) -> None:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

        storage = ObjectStorage(settings())
        monkeypatch.setattr(storage, "_put", boom)
        with pytest.raises(StorageUnavailable):
            await storage.put(
                document_key(TENANT, SHIPMENT, DOCUMENT, DocumentFormat.PDF),
                b"x",
                content_type="application/pdf",
            )

        written = capsys.readouterr().out
        assert str(DOCUMENT) in written, "отказ должен опознаваться хоть по чему-то"
        assert str(TENANT) not in written
