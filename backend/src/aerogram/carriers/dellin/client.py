"""HTTP-клиент Деловых Линий: ключ приложения и сессия личного кабинета.

Контур сверен по официальной OpenAPI 3.0.3 перевозчика
(``docs/integrations/sources/dellin/schema.yaml``, ADR-0020):

* **сервер один** — ``https://api.dellin.ru``. Тестового контура в ``servers``
  нет вовсе, и это не пропуск выгрузки: Swagger UI перевозчика предупреждает,
  что «любые операции, связанные с заказами, затрагивают реальные данные».
  Отсюда правило: ни один тест сюда не ходит, фикстуры синтетические
  и помеченные (ADR-0010);
* ``appkey`` — ключ приложения, передаётся **в теле** каждого запроса,
  а не заголовком;
* ``sessionID`` — идентификатор сессии личного кабинета, «срок действия
  сессии — 30 дней». Получается двумя способами:
  ``POST /v4/auth/login`` по токену (``pat``) или ``POST /v3/auth/login``
  по логину и паролю. Токен предпочтительнее: пароль от кабинета клиента
  хранить незачем, когда перевозчик даёт отзываемый токен.

Ошибки приходят конвертом ``{"metadata": {"status": …}, "errors": [...]}``,
причём в примерах спеки ключ написан то ``metadata``, то ``MetaData`` —
разбираются оба.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import httpx

from aerogram.carriers.http import CarrierHttpClient
from aerogram.shared.errors import CarrierAuthError, CarrierError, CarrierValidationError
from aerogram.shared.logging import get_logger

__all__ = [
    "BASE_URL",
    "LOGIN_CREDENTIALS_PATH",
    "LOGIN_PAT_PATH",
    "SESSION_INFO_PATH",
    "DellinClient",
    "dellin_error",
]

log = get_logger(__name__)

DELLIN_CODE: Final = "dellin"

#: Единственный сервер из ``servers`` официальной спеки. Тестового нет.
BASE_URL: Final = "https://api.dellin.ru"

LOGIN_PAT_PATH: Final = "/v4/auth/login.json"
LOGIN_CREDENTIALS_PATH: Final = "/v3/auth/login.json"
SESSION_INFO_PATH: Final = "/v3/auth/session_info.json"


def dellin_error(body: dict[str, Any]) -> str | None:
    """Человеческое описание ошибки из конверта перевозчика.

    ``None`` — ошибок нет. Возвращается именно текст, а не исключение:
    решение о том, что делать с ошибкой, принимает вызывающий, а некоторые
    ответы содержат ошибки вместе с полезными данными.
    """
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    parts: list[str] = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        detail = str(item.get("detail") or "").strip()
        fields = item.get("fields")
        text = " ".join(p for p in (title, detail) if p)
        if isinstance(fields, list) and fields:
            text = f"{text} ({', '.join(str(f) for f in fields)})".strip()
        if text:
            parts.append(text)
    return "; ".join(parts) or "Деловые Линии вернули ошибку без описания"


class DellinClient:
    """Клиент Деловых Линий с автоматическим получением сессии.

    Один экземпляр обслуживает одну учётную запись: сессия принадлежит паре
    «ключ приложения × пользователь кабинета» и между тенантами не делится.

    Сессия нужна не всегда. Расчёт объявлен со свободным ``sessionID``:
    без него возвращаются публичные тарифы, с ним — персональные скидки
    контрагента. Поэтому режим договора решает, обязательна ли она, —
    см. ``DellinAdapter``.
    """

    def __init__(
        self,
        *,
        appkey: str,
        pat: str | None = None,
        login: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not appkey:
            raise CarrierValidationError(
                "В учётной записи Деловых Линий не задан ключ приложения",
                carrier_code=DELLIN_CODE,
            )
        self._appkey = appkey
        self._pat = pat
        self._login = login
        self._password = password
        self._http = CarrierHttpClient(
            base_url=BASE_URL,
            carrier_code=DELLIN_CODE,
            timeout_seconds=timeout_seconds,
            client=http_client,
        )
        self._session_id: str | None = None
        # Параллельные расчёты одной учётной записью не должны порождать
        # несколько одновременных авторизаций.
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> DellinClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def can_authorize(self) -> bool:
        """Есть ли чем получить сессию личного кабинета."""
        return bool(self._pat) or bool(self._login and self._password)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def session(self) -> str:
        """Действующий идентификатор сессии, при необходимости — новый."""
        if self._session_id is not None:
            return self._session_id
        async with self._auth_lock:
            if self._session_id is not None:
                return self._session_id
            return await self._authorize()

    async def _authorize(self) -> str:
        if self._pat:
            path, payload = LOGIN_PAT_PATH, {"appkey": self._appkey, "pat": self._pat}
        elif self._login and self._password:
            path, payload = (
                LOGIN_CREDENTIALS_PATH,
                {"appkey": self._appkey, "login": self._login, "password": self._password},
            )
        else:
            raise CarrierValidationError(
                "Для входа в личный кабинет Деловых Линий нужен токен или пара логин-пароль",
                carrier_code=DELLIN_CODE,
            )

        # Повтор бесполезен при неверных данных и вреден при верных:
        # каждая попытка создаёт сессию.
        response = await self._http.request(
            "POST", path, operation="auth", json=payload, retry=False
        )
        body = self._body(response)

        message = dellin_error(body)
        if message:
            raise CarrierAuthError(message, carrier_code=DELLIN_CODE)

        data = body.get("data")
        session_id = data.get("sessionID") if isinstance(data, dict) else None
        if not session_id:
            raise CarrierAuthError(
                "Деловые Линии не вернули идентификатор сессии", carrier_code=DELLIN_CODE
            )

        self._session_id = str(session_id)
        # Ни ключ приложения, ни токен, ни сам идентификатор сессии в лог
        # не попадают: это учётные данные тенанта (CLAUDE.md §6).
        log.info("dellin.authorized", by="pat" if self._pat else "password")
        return self._session_id

    def invalidate_session(self) -> None:
        """Забыть сессию. Вызывается, когда перевозчик её отверг."""
        self._session_id = None

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        operation: str,
        with_session: bool = True,
        on_raw_call: Any = None,
    ) -> dict[str, Any]:
        """``POST`` с ключом приложения и, если нужно, идентификатором сессии.

        Один повтор после переавторизации отличает истёкшую сессию (её срок
        30 дней, и процесс живёт дольше) от неверных учётных данных, при
        которых повтор бесполезен.
        """
        for attempt in (1, 2):
            body_out: dict[str, Any] = {"appkey": self._appkey, **payload}
            if with_session:
                body_out["sessionID"] = await self.session()
            try:
                response = await self._http.request(
                    "POST", path, operation=operation, json=body_out, on_raw_call=on_raw_call
                )
            except CarrierAuthError:
                if not with_session or attempt == 2:
                    raise
                self.invalidate_session()
                log.warning("dellin.session_rejected_retrying", operation=operation)
                continue
            return self._body(response)

        raise CarrierAuthError(carrier_code=DELLIN_CODE)

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        body = response.json()
        if not isinstance(body, dict):
            raise CarrierError("Неожиданный формат ответа Деловых Линий", carrier_code=DELLIN_CODE)
        return body
