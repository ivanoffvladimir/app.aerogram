"""HTTP-клиент СДЭК: авторизация OAuth и кэш токена.

Контур сверен по исходникам SDK СДЭК для API 2.0 (см. ADR-0010):

* боевой контур ``https://api.cdek.ru/v2/``, тестовый ``https://api.edu.cdek.ru/v2/``;
* авторизация — ``POST oauth/token?parameters`` с телом
  ``application/x-www-form-urlencoded``: ``grant_type=client_credentials``,
  ``client_id``, ``client_secret``; ответ содержит ``access_token``
  и ``expires_in`` (по умолчанию 3600 секунд);
* дальнейшие вызовы — заголовок ``Authorization: Bearer <token>``.

Токен кэшируется в памяти процесса. Это осознанно проще, чем общий кэш
в Redis: токен живёт час, а стоимость лишней авторизации — один запрос
на процесс в час. Общий кэш добавил бы точку отказа ради экономии,
которой не видно.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

import httpx

from aerogram.carriers.http import CarrierHttpClient
from aerogram.shared.errors import CarrierAuthError, CarrierError
from aerogram.shared.logging import get_logger

__all__ = ["OAUTH_PATH", "PROD_BASE_URL", "SANDBOX_BASE_URL", "CdekClient"]

log = get_logger(__name__)

PROD_BASE_URL: Final = "https://api.cdek.ru/v2"
SANDBOX_BASE_URL: Final = "https://api.edu.cdek.ru/v2"

#: Путь авторизации именно такой, вместе с ``?parameters``: так он объявлен
#: в документации и в SDK СДЭК. Без хвоста контур отвечает 404.
OAUTH_PATH: Final = "/oauth/token?parameters"

#: Запас перед истечением токена. Без него запрос, отправленный за миг
#: до окончания срока, приходит уже с просроченным токеном.
TOKEN_EXPIRY_MARGIN_SECONDS: Final = 30


class CdekClient:
    """Клиент СДЭК с автоматической авторизацией.

    Один экземпляр обслуживает одну учётную запись: токен принадлежит паре
    «клиент × контур», и разделять его между тенантами нельзя.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        is_sandbox: bool = True,
        timeout_seconds: float = 3.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = SANDBOX_BASE_URL if is_sandbox else PROD_BASE_URL
        self._http = CarrierHttpClient(
            base_url=self._base_url,
            carrier_code="cdek",
            timeout_seconds=timeout_seconds,
            client=http_client,
        )
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Параллельный расчёт по нескольким запросам не должен приводить
        # к нескольким одновременным авторизациям одной учётной записью.
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> CdekClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_valid_token(self) -> bool:
        return self._token is not None and time.monotonic() < self._expires_at

    async def token(self) -> str:
        """Действующий токен, при необходимости — новый."""
        if self.has_valid_token:
            assert self._token is not None  # noqa: S101  # гарантировано has_valid_token
            return self._token

        async with self._auth_lock:
            # Пока ждали блокировку, токен мог получить кто-то другой.
            if self.has_valid_token:
                assert self._token is not None  # noqa: S101
                return self._token
            return await self._authorize()

    async def _authorize(self) -> str:
        response = await self._http.request(
            "POST",
            OAUTH_PATH,
            operation="auth",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            retry=False,
        )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise CarrierAuthError(carrier_code="cdek")

        expires_in = int(payload.get("expires_in") or 3600)
        self._token = str(token)
        self._expires_at = time.monotonic() + max(expires_in - TOKEN_EXPIRY_MARGIN_SECONDS, 0)
        log.info("cdek.authorized", expires_in=expires_in, sandbox=self._base_url != PROD_BASE_URL)
        return self._token

    def invalidate_token(self) -> None:
        """Сбросить токен. Вызывается, когда СДЭК ответил 401 на рабочий вызов."""
        self._token = None
        self._expires_at = 0.0

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        operation: str,
        on_raw_call: Any = None,
    ) -> dict[str, Any]:
        """Вызов с авторизацией и однократной переавторизацией на 401.

        Токен мог быть отозван в личном кабинете или устареть из-за
        рассинхронизации часов. Один повтор после переавторизации отличает
        этот случай от неверных учётных данных, при которых повтор бесполезен.
        """
        for attempt in (1, 2):
            token = await self.token()
            try:
                response = await self._http.request(
                    "POST",
                    path,
                    operation=operation,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    on_raw_call=on_raw_call,
                )
            except CarrierAuthError:
                self.invalidate_token()
                if attempt == 2:
                    raise
                log.warning("cdek.token_rejected_retrying", operation=operation)
                continue

            body = response.json()
            if not isinstance(body, dict):
                raise CarrierError("Неожиданный формат ответа СДЭК", carrier_code="cdek")
            return body

        raise CarrierAuthError(carrier_code="cdek")
