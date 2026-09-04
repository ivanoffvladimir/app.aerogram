"""HTTP-клиент ПЭК: Basic-аутентификация и ловушка «ошибка с кодом 200».

Контур сверен по официальной документации перевозчика (ADR-0020):

* базовый URL — ``https://kabinet.pecom.ru/api/v1/``, тестовый —
  ``https://test-kabinet.pecom.ru/preweb/api/v1/``. Оба взяты не из прозы,
  а из официального «Pecom Kabinet SDK» за подписью ``@author pecom.ru``;
* «Обращение к методам API осуществляется путём отправки запроса методом
  **POST** на URL метода» вида ``/<группа>/<метод>/``;
* «Аутентификация осуществляется с помощью Basic-аутентификации, в качестве
  логина необходимо использовать имя пользователя личного кабинета клиента
  «ПЭК», в качестве пароля — любой из активных ключей доступа»;
* «Общее ограничение на количество любых запросов по API: **100 запросов
  в минуту**».

**Главная ловушка контракта.** Документация: «Если возникает логическая
ошибка (не указаны необходимые параметры, неверный формат входных данных
и т.п.) возвращается ответ **с кодом 200** и описанием ошибки в формате
JSON: ``{"error": {"title": …, "message": …}}``». То есть проверять код
состояния недостаточно — тело обязано проверяться всегда. Ошибка, принятая
за успех, здесь тише всего и дороже всего: расчёт вернёт пустоту, а платформа
решит, что перевозчик просто не даёт тарифов.
"""

from __future__ import annotations

import base64
from typing import Any, Final

import httpx

from aerogram.carriers.http import CarrierHttpClient
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.logging import get_logger

__all__ = [
    "PROD_BASE_URL",
    "RATE_LIMIT_PER_MINUTE",
    "SANDBOX_BASE_URL",
    "PecomClient",
    "pecom_error",
]

log = get_logger(__name__)

PECOM_CODE: Final = "pecom"

PROD_BASE_URL: Final = "https://kabinet.pecom.ru/api/v1"
SANDBOX_BASE_URL: Final = "https://test-kabinet.pecom.ru/preweb/api/v1"

#: Ограничение перевозчика, названное в его документации. Здесь оно только
#: задокументировано: соблюдать его — забота вызывающего слоя, у которого
#: есть общая на процесс очередь, а не одиночного клиента.
RATE_LIMIT_PER_MINUTE: Final = 100


def pecom_error(body: dict[str, Any]) -> str | None:
    """Текст логической ошибки из тела ответа. ``None`` — ошибки нет.

    Разбирает оба места, где ПЭК её сообщает: конверт ``error`` с кодом 200
    и признак ``hasError`` с текстом ``errorMessage`` в ответе расчёта.
    """
    error = body.get("error")
    if isinstance(error, dict):
        title = str(error.get("title") or "").strip()
        message = str(error.get("message") or "").strip()
        text = " ".join(part for part in (title, message) if part)
        return text or "ПЭК вернул ошибку без описания"
    if body.get("hasError") is True:
        return str(body.get("errorMessage") or "").strip() or "ПЭК вернул ошибку без описания"
    return None


class PecomClient:
    """Клиент ПЭК. Один экземпляр — одна учётная запись."""

    def __init__(
        self,
        *,
        login: str,
        api_key: str,
        is_sandbox: bool = True,
        timeout_seconds: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not login or not api_key:
            raise CarrierValidationError(
                "В учётной записи ПЭК не заданы логин личного кабинета или ключ API",
                carrier_code=PECOM_CODE,
            )
        self._base_url = SANDBOX_BASE_URL if is_sandbox else PROD_BASE_URL
        # Basic-заголовок собирается один раз. Ни логин, ни ключ не попадают
        # ни в лог, ни в снимок вызова (CLAUDE.md §6).
        token = base64.b64encode(f"{login}:{api_key}".encode()).decode("ascii")
        self._auth_header = f"Basic {token}"
        self._http = CarrierHttpClient(
            base_url=self._base_url,
            carrier_code=PECOM_CODE,
            timeout_seconds=timeout_seconds,
            client=http_client,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> PecomClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def base_url(self) -> str:
        return self._base_url

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        operation: str,
        on_raw_call: Any = None,
    ) -> dict[str, Any]:
        """``POST`` метода API.

        Тело проверяется на логическую ошибку всегда, а не только при
        неуспешном коде: см. строку документации модуля.
        """
        response = await self._http.request(
            "POST",
            path,
            operation=operation,
            json=payload,
            headers={"Authorization": self._auth_header},
            on_raw_call=on_raw_call,
        )
        body = response.json()
        if isinstance(body, list):
            # Справочники ПЭК отдаются голым массивом. Оборачиваем, чтобы
            # вызывающий имел один тип на все методы.
            return {"items": body}
        if not isinstance(body, dict):
            raise CarrierError("Неожиданный формат ответа ПЭК", carrier_code=PECOM_CODE)

        message = pecom_error(body)
        if message:
            raise CarrierError(message, carrier_code=PECOM_CODE)
        return body

    async def post_raw(self, path: str, payload: dict[str, Any], *, operation: str) -> object:
        """``POST``, возвращающий разобранный JSON как есть.

        Нужен ровно одному методу — ``/order/print/``, формат ответа которого
        в документации записан неоднозначно (``{ "JVBERi0xLjQKJe..." }``,
        что корректным JSON не является). Оборачивать такой ответ в словарь
        значило бы потерять то единственное, что в нём есть.
        """
        response = await self._http.request(
            "POST",
            path,
            operation=operation,
            json=payload,
            headers={"Authorization": self._auth_header},
        )
        body = response.json()
        if isinstance(body, dict):
            message = pecom_error(body)
            if message:
                raise CarrierError(message, carrier_code=PECOM_CODE)
        return body
