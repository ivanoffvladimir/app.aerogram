"""Зависимости модуля справочников."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends

from aerogram.config import Settings, get_settings
from aerogram.directories.dadata import DadataClient

__all__ = ["DadataDep"]


async def get_dadata_client(settings: Settings | None = None) -> AsyncIterator[DadataClient | None]:
    """Клиент ДаData, либо ``None``, если токен не настроен.

    Отсутствие токена НЕ роняет приложение, хотя раздел 9.2 ТЗ требует падать
    при отсутствии обязательной переменной: токен ДаData обязательным не
    является. Он числится нерешённым блокером в docs/status.md, и падение
    старта означало бы полный отказ продукта вместо частичной деградации
    одной функции. Это тот же путь исполнения, что и недоступность ДаData.
    """
    cfg = settings or get_settings()
    if not cfg.dadata_token:
        yield None
        return

    client = DadataClient(token=cfg.dadata_token, secret=cfg.dadata_secret)
    try:
        yield client
    finally:
        await client.aclose()


DadataDep = Annotated["DadataClient | None", Depends(get_dadata_client)]
