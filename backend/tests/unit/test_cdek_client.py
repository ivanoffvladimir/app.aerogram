"""Клиент СДЭК: авторизация, кэш токена, переавторизация.

Сеть не используется. Домены СДЭК в контуре разработки закрыты сетевой
политикой, а боевые доступы не получены — см. tests/fixtures/cdek/README.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from aerogram.carriers.cdek.client import (
    OAUTH_PATH,
    PROD_BASE_URL,
    SANDBOX_BASE_URL,
    CdekClient,
)
from aerogram.shared.errors import CarrierAuthError, CarrierValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cdek"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: object) -> CdekClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(transport=transport, base_url=SANDBOX_BASE_URL)
    return CdekClient(
        client_id="test-id",
        client_secret="test-secret",
        http_client=inner,
        **kwargs,  # type: ignore[arg-type]
    )


class TestAuthorization:
    async def test_obtains_token_on_first_call(self) -> None:
        client = _client(lambda _: httpx.Response(200, json=load("oauth_ok")))
        token = await client.token()
        await client.aclose()
        assert token.startswith("eyJ")

    async def test_sends_form_encoded_client_credentials(self) -> None:
        """Авторизация СДЭК принимает форму, а не JSON.

        Отправка JSON приводит к 400 с невнятным текстом, и ошибка выглядит
        как «неверные учётные данные».
        """
        captured: dict[str, str] = {}
        body: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.headers)
            body["content"] = request.content.decode()
            return httpx.Response(200, json=load("oauth_ok"))

        client = _client(handler)
        await client.token()
        await client.aclose()

        assert captured["content-type"] == "application/x-www-form-urlencoded"
        assert "grant_type=client_credentials" in body["content"]
        assert "client_id=test-id" in body["content"]
        assert "client_secret=test-secret" in body["content"]

    async def test_uses_the_documented_oauth_path(self) -> None:
        # Путь именно с хвостом ?parameters: без него контур отвечает 404.
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.raw_path.decode())
            return httpx.Response(200, json=load("oauth_ok"))

        client = _client(handler)
        await client.token()
        await client.aclose()
        assert OAUTH_PATH.split("?")[0] in paths[0]
        assert "parameters" in paths[0]

    async def test_rejected_credentials_give_auth_error(self) -> None:
        client = _client(lambda _: httpx.Response(401, json={"message": "нет"}))
        with pytest.raises(CarrierAuthError):
            await client.token()
        await client.aclose()

    async def test_response_without_token_is_auth_error(self) -> None:
        # 200 без access_token — не успех: дальше пошёл бы Bearer None.
        client = _client(lambda _: httpx.Response(200, json={"expires_in": 3600}))
        with pytest.raises(CarrierAuthError):
            await client.token()
        await client.aclose()


class TestTokenCache:
    async def test_token_is_reused_within_its_lifetime(self) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=load("oauth_ok"))

        client = _client(handler)
        first = await client.token()
        second = await client.token()
        await client.aclose()

        assert first == second
        assert calls == 1, "повторная авторизация при живом токене — лишний запрос к СДЭК"

    async def test_expired_token_is_renewed(self) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"access_token": f"t{calls}", "expires_in": 3600})

        client = _client(handler)
        await client.token()
        # Имитируем истечение, не трогая часы процесса.
        client._expires_at = 0.0
        second = await client.token()
        await client.aclose()

        assert calls == 2
        assert second == "t2"

    async def test_short_lived_token_is_treated_as_expired_early(self) -> None:
        """Запас перед истечением обязателен.

        Токен со сроком меньше запаса считается просроченным сразу: иначе
        запрос, отправленный за миг до конца срока, придёт уже с мёртвым
        токеном, и это будет выглядеть как отказ авторизации.
        """
        client = _client(lambda _: httpx.Response(200, json={"access_token": "t", "expires_in": 5}))
        await client.token()
        await client.aclose()
        assert client.has_valid_token is False

    async def test_parallel_calls_authorize_only_once(self) -> None:
        """Параллельный расчёт не должен давать нескольких авторизаций.

        СДЭК ограничивает частоту обращений, и сжигать лимит на дублирующей
        авторизации — верный способ получить 429 в середине выдачи.
        """
        import asyncio

        calls = 0

        async def slow_handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json=load("oauth_ok"))

        transport = httpx.MockTransport(slow_handler)
        inner = httpx.AsyncClient(transport=transport, base_url=SANDBOX_BASE_URL)
        client = CdekClient(client_id="i", client_secret="s", http_client=inner)

        await asyncio.gather(*(client.token() for _ in range(5)))
        await client.aclose()
        assert calls == 1


class TestReauthorisationOnRejectedToken:
    async def test_retries_once_after_401_on_a_working_call(self) -> None:
        """Токен мог быть отозван в личном кабинете до истечения срока."""
        auth_calls = 0
        work_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal auth_calls, work_calls
            if "oauth" in str(request.url):
                auth_calls += 1
                return httpx.Response(
                    200, json={"access_token": f"t{auth_calls}", "expires_in": 3600}
                )
            work_calls += 1
            if work_calls == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"tariff_codes": []})

        client = _client(handler)
        body = await client.post("/calculator/tarifflist", {}, operation="quote")
        await client.aclose()

        assert body == {"tariff_codes": []}
        assert auth_calls == 2, "после отказа токен обязан быть перезапрошен"

    async def test_gives_up_after_second_401(self) -> None:
        # Повторный отказ означает неверные учётные данные, а не старый токен.
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in str(request.url):
                return httpx.Response(200, json=load("oauth_ok"))
            return httpx.Response(401)

        client = _client(handler)
        with pytest.raises(CarrierAuthError):
            await client.post("/calculator/tarifflist", {}, operation="quote")
        await client.aclose()


class TestEnvironment:
    def test_sandbox_is_the_default(self) -> None:
        client = CdekClient(client_id="i", client_secret="s")
        assert client.base_url == SANDBOX_BASE_URL

    def test_production_is_explicit(self) -> None:
        client = CdekClient(client_id="i", client_secret="s", is_sandbox=False)
        assert client.base_url == PROD_BASE_URL

    def test_environments_are_different_hosts(self) -> None:
        # Перепутанный контур означает боевые заказы на тестовом стенде.
        assert PROD_BASE_URL != SANDBOX_BASE_URL
        assert "edu" in SANDBOX_BASE_URL


class TestCredentialValidation:
    def test_missing_credentials_are_reported_before_any_request(self) -> None:
        from aerogram.carriers.base import CarrierAccount
        from aerogram.carriers.cdek.adapter import CdekAdapter

        account = CarrierAccount(
            account_id="1", carrier_code="cdek", mode="own_contract", credentials={}
        )
        with pytest.raises(CarrierValidationError, match="client_id"):
            CdekAdapter._default_client(account)
