"""Клиент ДаData: разбор ответов, ошибки, повторы, отсутствие ПДн в логах.

Сеть не используется: транспорт подменяется управляемым, поэтому тесты
детерминированы и идут в CI без доступа наружу. Домен ДаData в контуре
разработки к тому же закрыт egress-политикой.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from aerogram.directories.dadata import DadataClient
from aerogram.shared.errors import (
    DirectoryAuthError,
    DirectoryError,
    DirectoryQuotaExceeded,
    DirectoryUnavailable,
)

SUGGEST_BODY = {
    "suggestions": [
        {
            "value": "г Новосибирск",
            "unrestricted_value": "630000, Новосибирская обл, г Новосибирск",
            "data": {
                "country_iso_code": "RU",
                "region": "Новосибирская",
                "region_with_type": "Новосибирская обл",
                "region_fias_id": "1ac46b49-3209-4814-b7bf-a509ea1aecd9",
                "city": "Новосибирск",
                "city_with_type": "г Новосибирск",
                "city_fias_id": "8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                "city_kladr_id": "5400000100000",
                "fias_id": "8dea00e3-9aab-4d8e-887c-ef2aaa546456",
                "fias_level": "4",
                "postal_code": "630000",
                "timezone": "UTC+7",
            },
        }
    ]
}


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: object) -> DadataClient:
    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DadataClient(token="test-token", client=inner, **kwargs)  # type: ignore[arg-type]


class TestSuggestAddress:
    async def test_parses_suggestion(self) -> None:
        client = _client(lambda _: httpx.Response(200, json=SUGGEST_BODY))
        items = await client.suggest_address("новосиб")
        await client.aclose()

        assert len(items) == 1
        assert items[0].data.city_fias_id == "8dea00e3-9aab-4d8e-887c-ef2aaa546456"
        assert items[0].data.fias_level == "4"

    async def test_sends_token_and_never_the_secret(self) -> None:
        """В подсказках X-Secret не используется и уходить наружу не должен."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.headers)
            return httpx.Response(200, json=SUGGEST_BODY)

        client = _client(handler, secret="test-secret")
        await client.suggest_address("новосиб")
        await client.aclose()

        assert captured["authorization"] == "Token test-token"
        assert "x-secret" not in captured

    async def test_limits_query_length_and_count(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json=SUGGEST_BODY)

        client = _client(handler)
        await client.suggest_address("x" * 500, count=99)
        await client.aclose()

        assert len(str(captured["query"])) == 300
        assert captured["count"] == 20

    async def test_restricts_to_russia(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json=SUGGEST_BODY)

        client = _client(handler)
        await client.suggest_address("новосиб")
        await client.aclose()

        assert captured["locations"] == [{"country_iso_code": "RU"}]

    async def test_broken_suggestion_does_not_break_the_whole_list(self) -> None:
        """Девять подсказок лучше, чем ошибка формы из-за одной битой."""
        body = {"suggestions": [{"нет": "value"}, SUGGEST_BODY["suggestions"][0]]}
        client = _client(lambda _: httpx.Response(200, json=body))
        items = await client.suggest_address("новосиб")
        await client.aclose()

        assert len(items) == 1

    async def test_empty_response_is_not_an_error(self) -> None:
        client = _client(lambda _: httpx.Response(200, json={"suggestions": []}))
        assert await client.suggest_address("щьщьщь") == ()
        await client.aclose()


class TestErrorMapping:
    async def test_403_means_quota_exhausted_not_forbidden(self) -> None:
        """У ДаData исчерпанная суточная квота приходит как 403, а не 429."""
        client = _client(lambda _: httpx.Response(403, json={"message": "disabled"}))
        with pytest.raises(DirectoryQuotaExceeded):
            await client.suggest_address("новосиб")
        await client.aclose()

    async def test_401_is_auth_error(self) -> None:
        client = _client(lambda _: httpx.Response(401))
        with pytest.raises(DirectoryAuthError):
            await client.suggest_address("новосиб")
        await client.aclose()

    async def test_429_is_quota_error(self) -> None:
        client = _client(lambda _: httpx.Response(429))
        with pytest.raises(DirectoryQuotaExceeded):
            await client.suggest_address("новосиб")
        await client.aclose()

    async def test_unreadable_body_is_reported_clearly(self) -> None:
        client = _client(lambda _: httpx.Response(200, content="<html>не json</html>".encode()))
        with pytest.raises(DirectoryError, match="нечитаемый"):
            await client.suggest_address("новосиб")
        await client.aclose()

    async def test_directory_errors_are_502_not_500(self) -> None:
        # Плохо отвечает внешняя система, а не наша.
        assert DirectoryUnavailable().http_status == 502
        assert DirectoryQuotaExceeded().http_status == 502

    async def test_directory_error_has_no_carrier_code(self) -> None:
        """Ошибка справочника не должна выглядеть как ошибка перевозчика.

        Человеку, вводящему адрес, текст «Перевозчик вернул ошибку» ничего
        не объясняет и уводит разбор инцидента не в ту сторону.
        """
        payload = DirectoryUnavailable().as_payload("rq_1")
        assert payload["error"]["carrier_code"] is None  # type: ignore[index]
        assert "Перевозчик" not in payload["error"]["message"]  # type: ignore[index]


class TestRetries:
    async def test_retries_transient_failure(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503)
            return httpx.Response(200, json=SUGGEST_BODY)

        client = _client(handler)
        assert len(await client.suggest_address("новосиб")) == 1
        await client.aclose()
        assert attempts == 3

    async def test_never_retries_exhausted_quota(self) -> None:
        """Повтор при 403 только быстрее сожжёт остаток лимита."""
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(403)

        client = _client(handler)
        with pytest.raises(DirectoryQuotaExceeded):
            await client.suggest_address("новосиб")
        await client.aclose()
        assert attempts == 1

    async def test_never_retries_auth_error(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(401)

        client = _client(handler)
        with pytest.raises(DirectoryAuthError):
            await client.suggest_address("новосиб")
        await client.aclose()
        assert attempts == 1

    async def test_timeout_is_retried_then_reported(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("долго", request=request)

        client = _client(handler, max_attempts=2)
        with pytest.raises(DirectoryUnavailable):
            await client.suggest_address("новосиб")
        await client.aclose()
        assert attempts == 2


class TestCleaner:
    async def test_requires_secret(self) -> None:
        client = _client(lambda _: httpx.Response(200, json=[]))
        with pytest.raises(DirectoryAuthError, match="не настроена"):
            await client.clean_address("мск сухонская 11")
        await client.aclose()

    async def test_sends_both_credentials(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.headers)
            return httpx.Response(200, json=[{"city_fias_id": "x", "city": "Москва"}])

        client = _client(handler, secret="test-secret")
        await client.clean_address("мск сухонская 11")
        await client.aclose()

        assert captured["authorization"] == "Token test-token"
        assert captured["x-secret"] == "test-secret"

    async def test_reports_cleaner_availability(self) -> None:
        without = _client(lambda _: httpx.Response(200, json=[]))
        with_secret = _client(lambda _: httpx.Response(200, json=[]), secret="test-secret")
        assert without.has_cleaner_credentials is False
        assert with_secret.has_cleaner_credentials is True
        await without.aclose()
        await with_secret.aclose()


class TestPartyLookup:
    async def test_parses_legal_entity(self) -> None:
        body = {
            "suggestions": [
                {
                    "value": 'ООО "РОСПЛОМБА"',
                    "data": {
                        "inn": "7701234567",
                        "kpp": "770101001",
                        "type": "LEGAL",
                        "name": {"short_with_opf": 'ООО "РОСПЛОМБА"'},
                        "state": {"status": "ACTIVE"},
                        "address": {
                            "value": "г Москва, ул Тверская, д 1",
                            "data": {"city_fias_id": "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"},
                        },
                    },
                }
            ]
        }
        client = _client(lambda _: httpx.Response(200, json=body))
        draft = await client.find_party_by_inn("7701234567")
        await client.aclose()

        assert draft is not None
        assert draft.type == "legal"
        assert draft.inn == "7701234567"
        assert draft.kpp == "770101001"
        assert draft.city_fias_id == "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
        assert draft.is_active is True

    async def test_parses_entrepreneur(self) -> None:
        body = {
            "suggestions": [
                {
                    "value": "ИП Петров",
                    "data": {
                        "inn": "770123456789",
                        "type": "INDIVIDUAL",
                        "name": {"full_with_opf": "ИП Петров Иван Сергеевич"},
                        "state": {"status": "ACTIVE"},
                    },
                }
            ]
        }
        client = _client(lambda _: httpx.Response(200, json=body))
        draft = await client.find_party_by_inn("770123456789")
        await client.aclose()

        assert draft is not None
        assert draft.type == "entrepreneur"
        assert draft.kpp is None

    async def test_liquidated_company_is_marked_inactive(self) -> None:
        """Ликвидированное юрлицо возвращается, но помечается.

        Скрывать его нельзя: пользователь должен понять, почему контрагент
        не находится, а не гадать об опечатке в ИНН.
        """
        body = {
            "suggestions": [
                {
                    "value": "ООО «Прошлое»",
                    "data": {
                        "inn": "7709999999",
                        "type": "LEGAL",
                        "name": {"short_with_opf": "ООО «Прошлое»"},
                        "state": {"status": "LIQUIDATED"},
                    },
                }
            ]
        }
        client = _client(lambda _: httpx.Response(200, json=body))
        draft = await client.find_party_by_inn("7709999999")
        await client.aclose()

        assert draft is not None
        assert draft.is_active is False

    async def test_unknown_inn_gives_none(self) -> None:
        client = _client(lambda _: httpx.Response(200, json={"suggestions": []}))
        assert await client.find_party_by_inn("0000000000") is None
        await client.aclose()
