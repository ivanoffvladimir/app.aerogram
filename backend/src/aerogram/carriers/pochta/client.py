"""HTTP-клиент Почты России: два заголовка авторизации и ненайденный хост.

Контур сверен по официальной документации API Онлайн-сервиса «Отправка»
(планка ADR-0020, файлы в `docs/integrations/sources/pochta/otpravka/`).

**Авторизация двухсоставная и без единого сетевого вызова.** Документация:
«Для интеграции с API Онлайн-сервиса «Отправка» необходимо располагать
токеном авторизации приложения; ключом авторизации пользователя». Первый
идёт в `Authorization` с префиксом `AccessToken`, второй — в отдельном
заголовке `X-User-Authorization` с префиксом `Basic`. Эндпоинта логина нет
вовсе, поэтому клиент проще, чем у Деловых Линий: ни сессии, ни блокировки,
ни переавторизации после 401.

Ключ пользователя перевозчик просит собрать самостоятельно: «Ключ авторизации
пользователя генерируется с помощью алгоритма base64. Перед кодированием имя
и пароль, выданные сервисом passport.pochta.ru, разделяются двоеточием».
Поэтому клиент принимает либо пару логин-пароль, либо уже готовый ключ —
у клиента может не быть исходного пароля под рукой.

**Базового URL в официальном источнике нет, и выдумывать его нельзя.**
Все 117 страниц справки дают только «Локальный URL» вида `/1.0/tariff`.
Полный адрес встречается ровно в одном файле, в примерах curl, и это
`https://iplatform-extapi.test.russianpost.ru` — страница нигде не называет
его ни тестовым контуром, ни боевым. Отсюда решение: этот адрес используется
как песочница (единственный, который перевозчик написал сам), а боевой
задаётся в учётной записи. Пустой боевой адрес — отказ с внятным текстом,
а не тихий вызов «куда-нибудь»: планка ADR-0020 запрещает подставлять то,
чего в источнике нет.

**Единого формата ошибки у API нет.** Разные методы отвечают по-разному:
`{"error-code": …, "error-details": …}`, `{"code": …, "description": …}`,
массив `errors`, а иногда голой строкой кода. У расчёта формат ошибки
не описан вообще. Поэтому разбор перебирает известные формы и, не узнав
ни одной, честно говорит, что перевозчик отказал без описания, — но
никогда не выдаёт ошибку за успех.
"""

from __future__ import annotations

import base64
from typing import Any, Final

import httpx

from aerogram.carriers.http import CarrierHttpClient
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.logging import get_logger

__all__ = [
    "BASE_URL_SETTING",
    "POCHTA_CODE",
    "SANDBOX_BASE_URL",
    "USER_AUTH_HEADER",
    "PochtaClient",
    "pochta_error",
    "user_key",
]

log = get_logger(__name__)

POCHTA_CODE: Final = "pochta"

#: Единственный адрес API, названный самим перевозчиком: примеры curl
#: на странице `usecases-create_orders.html`. Тестовым его документация
#: не называет — см. строку документации модуля.
SANDBOX_BASE_URL: Final = "https://iplatform-extapi.test.russianpost.ru"

#: Заголовок с ключом пользователя. Обычный Basic на `Authorization`
#: у Почты не используется: там токен приложения.
USER_AUTH_HEADER: Final = "X-User-Authorization"

#: Ключ настройки учётной записи с боевым адресом API.
BASE_URL_SETTING: Final = "base_url"


def user_key(*, login: str | None, password: str | None, key: str | None) -> str:
    """Ключ авторизации пользователя: готовый или собранный из пары.

    Готовый ключ имеет приоритет: если тенант дал его, значит исходного
    пароля у нас может и не быть, и собирать нечего.
    """
    if key:
        return key.strip()
    if login and password:
        return base64.b64encode(f"{login}:{password}".encode()).decode("ascii")
    raise CarrierValidationError(
        "В учётной записи Почты России не заданы ни ключ авторизации пользователя, "
        "ни пара логин-пароль для его вычисления",
        carrier_code=POCHTA_CODE,
    )


def pochta_error(body: object) -> str | None:
    """Текст ошибки из тела ответа. ``None`` — ошибки в теле нет.

    Разбирает все формы, встречающиеся в документации: одиночный конверт
    с ``error-code``/``error-details``, конверт с ``code``/``description``,
    массив ``errors`` из таких конвертов и голую строку кода ошибки.
    """
    if isinstance(body, str):
        text = body.strip().strip('"')
        return text or None
    if isinstance(body, list):
        parts = [message for item in body if (message := pochta_error(item))]
        return "; ".join(parts) or None
    if not isinstance(body, dict):
        return None

    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        nested = pochta_error(errors)
        if nested:
            return nested

    code = body.get("error-code") or body.get("code") or body.get("error")
    detail = body.get("error-details") or body.get("description") or body.get("desc")
    # ``UNDEFINED`` — значение-плейсхолдер из схем документации, оно
    # приходит и в успешных ответах: ошибкой его считать нельзя.
    if isinstance(code, str) and code.strip() and code.strip() != "UNDEFINED":
        text = " ".join(
            part.strip() for part in (code, detail) if isinstance(part, str) and part.strip()
        )
        return text or None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return None


class PochtaClient:
    """Клиент API «Отправка». Один экземпляр — одна учётная запись."""

    def __init__(
        self,
        *,
        token: str,
        user_auth_key: str,
        base_url: str | None = None,
        is_sandbox: bool = True,
        timeout_seconds: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise CarrierValidationError(
                "В учётной записи Почты России не задан токен авторизации приложения",
                carrier_code=POCHTA_CODE,
            )
        if not user_auth_key:
            raise CarrierValidationError(
                "В учётной записи Почты России не задан ключ авторизации пользователя",
                carrier_code=POCHTA_CODE,
            )
        self._base_url = self._resolve_base_url(base_url, is_sandbox=is_sandbox)
        # Заголовки собираются один раз. Ни токен, ни ключ не попадают
        # ни в лог, ни в снимок вызова (CLAUDE.md §6).
        self._auth_headers = {
            "Authorization": f"AccessToken {token}",
            USER_AUTH_HEADER: f"Basic {user_auth_key}",
            # Кодировка указана перевозчиком явно и на каждой странице:
            # `application/json;charset=UTF-8`.
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json;charset=UTF-8",
        }
        self._http = CarrierHttpClient(
            base_url=self._base_url,
            carrier_code=POCHTA_CODE,
            timeout_seconds=timeout_seconds,
            client=http_client,
        )

    @staticmethod
    def _resolve_base_url(base_url: str | None, *, is_sandbox: bool) -> str:
        """Адрес API: заданный в учётной записи или песочница.

        Заданный побеждает всегда — в том числе в песочнице: у тенанта
        может быть свой стенд. Боевой режим без адреса — отказ, потому что
        боевого адреса Почта в документации не публикует.
        """
        if base_url and base_url.strip():
            return base_url.strip().rstrip("/")
        if is_sandbox:
            return SANDBOX_BASE_URL
        raise CarrierValidationError(
            "Для боевого режима Почты России нужно задать адрес API в настройках "
            "учётной записи: перевозчик не публикует его в документации",
            carrier_code=POCHTA_CODE,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> PochtaClient:
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
        """``POST`` метода API с разбором конверта ошибок."""
        response = await self._http.request(
            "POST",
            path,
            operation=operation,
            json=payload,
            headers=self._auth_headers,
            on_raw_call=on_raw_call,
        )
        return self._body(response)

    async def get(
        self,
        path: str,
        *,
        operation: str,
        on_raw_call: Any = None,
    ) -> dict[str, Any]:
        """``GET`` метода API с разбором конверта ошибок."""
        response = await self._http.request(
            "GET",
            path,
            operation=operation,
            headers=self._auth_headers,
            on_raw_call=on_raw_call,
        )
        return self._body(response)

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise CarrierError("Почта России вернула не JSON", carrier_code=POCHTA_CODE) from exc
        message = pochta_error(body)
        if message:
            raise CarrierError(message, carrier_code=POCHTA_CODE)
        if not isinstance(body, dict):
            raise CarrierError("Неожиданный формат ответа Почты России", carrier_code=POCHTA_CODE)
        return body
