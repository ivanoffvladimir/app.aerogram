"""Клиент Деловых Линий: ключ приложения, сессия и переавторизация.

Сеть не используется. Боевой контур у перевозчика единственный, тестового
в официальной спеке нет вовсе — поэтому транспорт подменяется целиком.
"""

from __future__ import annotations

import json

import httpx
import pytest

from aerogram.carriers.dellin.client import (
    BASE_URL,
    LOGIN_CREDENTIALS_PATH,
    LOGIN_PAT_PATH,
    DellinClient,
    dellin_error,
)
from aerogram.shared.errors import CarrierAuthError, CarrierValidationError

_SESSION_OK = {"metadata": {"status": 200}, "data": {"sessionID": "sess-1"}}


def _client(handler, **kwargs) -> DellinClient:  # type: ignore[no-untyped-def]
    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    kwargs.setdefault("appkey", "app-key")
    return DellinClient(http_client=inner, **kwargs)


def _ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_SESSION_OK)


class TestErrorEnvelope:
    def test_no_errors_is_none(self) -> None:
        assert dellin_error({"data": {}}) is None
        assert dellin_error({"errors": []}) is None

    def test_error_carries_title_detail_and_fields(self) -> None:
        message = dellin_error(
            {
                "errors": [
                    {
                        "code": 110003,
                        "title": "Отсутствует обязательный параметр",
                        "detail": "Это поле обязательно.",
                        "fields": ["login"],
                    }
                ]
            }
        )
        assert message is not None
        assert "Отсутствует обязательный параметр" in message
        assert "login" in message

    def test_error_without_text_still_reports_something(self) -> None:
        """Молчаливая ошибка хуже невнятной: её нечем показать оператору."""
        assert dellin_error({"errors": [{"code": 1}]}) == (
            "Деловые Линии вернули ошибку без описания"
        )


class TestConstruction:
    def test_appkey_is_required(self) -> None:
        with pytest.raises(CarrierValidationError, match="ключ приложения"):
            DellinClient(appkey="")

    def test_can_authorize_needs_token_or_pair(self) -> None:
        assert _client(_ok, pat="t").can_authorize
        assert _client(_ok, login="l", password="p").can_authorize
        assert not _client(_ok).can_authorize


@pytest.mark.anyio
class TestSession:
    async def test_token_login_is_preferred_over_password(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json=_SESSION_OK)

        client = _client(handler, pat="dl-api-token", login="l", password="p")
        assert await client.session() == "sess-1"
        assert seen == [LOGIN_PAT_PATH]

    async def test_password_login_when_no_token(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json=_SESSION_OK)

        client = _client(handler, login="l", password="p")
        assert await client.session() == "sess-1"
        assert seen == [LOGIN_CREDENTIALS_PATH]

    async def test_session_is_reused(self) -> None:
        """Сессия живёт 30 дней: авторизовываться на каждый вызов незачем."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if "login" in request.url.path:
                return httpx.Response(200, json=_SESSION_OK)
            return httpx.Response(200, json={"data": {"ok": True}})

        client = _client(handler, pat="t")
        await client.post("/v2/calculator.json", {}, operation="quote")
        await client.post("/v2/calculator.json", {}, operation="quote")
        assert calls.count(LOGIN_PAT_PATH) == 1

    async def test_appkey_and_session_travel_in_the_body(self) -> None:
        """У Деловых Линий ключ и сессия — поля тела, а не заголовки."""
        bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "login" in request.url.path:
                return httpx.Response(200, json=_SESSION_OK)
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {}})

        client = _client(handler, pat="t")
        await client.post("/v2/calculator.json", {"cargo": {}}, operation="quote")
        assert bodies[0]["appkey"] == "app-key"
        assert bodies[0]["sessionID"] == "sess-1"
        assert bodies[0]["cargo"] == {}

    async def test_expired_session_is_retried_once(self) -> None:
        """Сессия могла истечь: процесс живёт дольше тридцати дней."""
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.path)
            if "login" in request.url.path:
                return httpx.Response(200, json=_SESSION_OK)
            if attempts.count("/v2/calculator.json") == 1:
                return httpx.Response(401, json={"errors": [{"title": "Сессия истекла"}]})
            return httpx.Response(200, json={"data": {"ok": True}})

        client = _client(handler, pat="t")
        body = await client.post("/v2/calculator.json", {}, operation="quote")
        assert body == {"data": {"ok": True}}
        assert attempts.count(LOGIN_PAT_PATH) == 2

    async def test_wrong_credentials_do_not_loop(self) -> None:
        """При неверных данных повтор бесполезен и лишь удваивает нагрузку."""
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.path)
            return httpx.Response(401, json={"errors": [{"title": "Неверный токен"}]})

        client = _client(handler, pat="bad")
        with pytest.raises(CarrierAuthError):
            await client.post("/v2/calculator.json", {}, operation="quote")
        assert attempts.count(LOGIN_PAT_PATH) <= 2

    async def test_login_error_is_reported_not_swallowed(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"errors": [{"title": "Неверный логин"}]})

        client = _client(handler, pat="t")
        with pytest.raises(CarrierAuthError, match="Неверный логин"):
            await client.session()

    async def test_login_without_session_id_is_an_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"metadata": {"status": 200}, "data": {}})

        client = _client(handler, pat="t")
        with pytest.raises(CarrierAuthError, match="идентификатор сессии"):
            await client.session()

    async def test_no_credentials_means_no_session(self) -> None:
        client = _client(_ok)
        with pytest.raises(CarrierValidationError, match="токен"):
            await client.session()
