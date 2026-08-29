"""Клиент ДаData: подсказки адресов и поиск организаций по ИНН.

Отдельный клиент, а не ``carriers.http.CarrierHttpClient``. Причины, каждой
достаточно по отдельности:

* тот клиент поднимает ``Carrier*``-ошибки, и пользователь, вводящий адрес,
  получил бы в ответе ``carrier_code`` и текст «Перевозчик вернул ошибку»;
* тот клиент снимает сырьё вызова с телами запроса и ответа и складывает его
  в ``carrier_raw_calls`` на 30 суток — а тело запроса к ДаData это адрес
  получателя, то есть персональные данные (12.7 ТЗ);
* политика повторов другая: у ДаData 403 означает исчерпанную квоту и
  повтором не лечится, а 429 повтором только продлевается.

Персональные данные: строка запроса **никогда не пишется в лог**. В логи идут
длина запроса и код ответа — этого достаточно, чтобы разобрать инцидент,
и недостаточно, чтобы узнать, куда клиент отправляет груз.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from aerogram.directories.schemas import DadataAddressData, DadataSuggestion, PartyDraft
from aerogram.shared.errors import (
    DirectoryAuthError,
    DirectoryError,
    DirectoryQuotaExceeded,
    DirectoryUnavailable,
)
from aerogram.shared.logging import get_logger

__all__ = ["CLEANER_BASE_URL", "SUGGEST_BASE_URL", "DadataClient"]

log = get_logger(__name__)

SUGGEST_BASE_URL: Final = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
CLEANER_BASE_URL: Final = "https://cleaner.dadata.ru/api/v1"

#: Максимальная длина запроса, которую принимает ДаData.
MAX_QUERY_LENGTH: Final = 300
#: Потолок count в подсказках.
MAX_SUGGESTION_COUNT: Final = 20

#: Коды, при которых повтор осмыслен. 403 и 429 сюда намеренно не входят.
_RETRYABLE_STATUSES: Final = frozenset({500, 502, 503, 504})


class DadataClient:
    """HTTP-клиент ДаData.

    Клиент ничего не кэширует и не знает о квотах: и то и другое — состояние,
    общее для всех процессов приложения, и живёт оно в сервисном слое поверх
    Redis. Здесь остаётся ровно один вызов и разбор его результата, что делает
    клиент проверяемым на подменённом транспорте без сети.
    """

    def __init__(
        self,
        *,
        token: str,
        secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 3.0,
        connect_timeout_seconds: float = 2.0,
        max_attempts: int = 3,
    ) -> None:
        self._token = token
        self._secret = secret
        self._max_attempts = max_attempts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> DadataClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def has_cleaner_credentials(self) -> bool:
        """Доступна ли стандартизация: ей нужен ещё и секретный ключ."""
        return self._secret is not None

    async def suggest_address(
        self,
        query: str,
        *,
        count: int = 10,
        from_bound: str = "city",
        to_bound: str = "settlement",
    ) -> tuple[DadataSuggestion, ...]:
        """Подсказки адреса.

        По умолчанию выдача ограничена диапазоном «город — населённый пункт»:
        для выбора пункта назначения улица и дом не нужны, а каждый лишний
        уровень — это лишние строки в выдаче и лишний расход суточной квоты.
        """
        payload: dict[str, Any] = {
            "query": query[:MAX_QUERY_LENGTH],
            "count": min(count, MAX_SUGGESTION_COUNT),
            "from_bound": {"value": from_bound},
            "to_bound": {"value": to_bound},
            "locations": [{"country_iso_code": "RU"}],
            "language": "ru",
        }
        body = await self._post(f"{SUGGEST_BASE_URL}/suggest/address", payload, operation="suggest")
        return self._parse_suggestions(body)

    async def find_address_by_fias(self, fias_id: str) -> DadataSuggestion | None:
        """Карточка объекта ФИАС по идентификатору.

        Нужна, чтобы дозаполнить справочник городов, когда пользователь выбрал
        населённый пункт, которого у нас ещё нет.
        """
        payload = {"query": fias_id, "count": 1}
        body = await self._post(
            f"{SUGGEST_BASE_URL}/findById/address", payload, operation="find_address"
        )
        suggestions = self._parse_suggestions(body)
        return suggestions[0] if suggestions else None

    async def find_party_by_inn(self, inn: str, kpp: str | None = None) -> PartyDraft | None:
        """Организация по ИНН — для заполнения контрагента (FR-8.4).

        Возвращается черновик, а не готовая запись: решение о заведении
        контрагента принимает человек, увидев данные реестра.
        """
        payload: dict[str, Any] = {"query": inn, "count": 1}
        if kpp:
            payload["kpp"] = kpp
        body = await self._post(f"{SUGGEST_BASE_URL}/findById/party", payload, operation="party")

        suggestions = body.get("suggestions") or []
        if not suggestions:
            return None
        return self._parse_party(suggestions[0])

    async def clean_address(self, query: str) -> DadataAddressData | None:
        """Стандартизация адреса.

        Требует секретного ключа и тарифицируется отдельно, поэтому вызывается
        только на пути создания отправления, а не на каждое нажатие клавиши.
        """
        if self._secret is None:
            raise DirectoryAuthError("Стандартизация адресов не настроена")

        body = await self._post(
            f"{CLEANER_BASE_URL}/clean/address",
            [query[:MAX_QUERY_LENGTH]],
            operation="clean",
            with_secret=True,
        )
        rows = body if isinstance(body, list) else body.get("data") or []
        if not rows:
            return None
        return DadataAddressData.model_validate(rows[0])

    async def _post(
        self,
        url: str,
        payload: Any,
        *,
        operation: str,
        with_secret: bool = False,
    ) -> Any:
        """Выполнить запрос с повторами и разбором ошибок."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Token {self._token}",
        }
        if with_secret and self._secret is not None:
            headers["X-Secret"] = self._secret

        last_error: DirectoryError | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = DirectoryUnavailable("Справочник адресов не ответил вовремя")
                log.warning("dadata.timeout", operation=operation, attempt=attempt)
            except httpx.HTTPError as exc:
                last_error = DirectoryUnavailable()
                log.warning(
                    "dadata.transport_error",
                    operation=operation,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
            else:
                error = self._error_for_status(response)
                if error is None:
                    return self._parse_body(response)
                last_error = error
                log.warning(
                    "dadata.http_error",
                    operation=operation,
                    attempt=attempt,
                    status=response.status_code,
                    code=error.code,
                )
                if response.status_code not in _RETRYABLE_STATUSES:
                    raise error

            if attempt == self._max_attempts:
                break
            await asyncio.sleep(0.2 * 2 ** (attempt - 1))

        raise last_error or DirectoryUnavailable()

    def _error_for_status(self, response: httpx.Response) -> DirectoryError | None:
        status = response.status_code
        if status < 400:
            return None
        if status == 401:
            return DirectoryAuthError()
        if status == 403:
            # У ДаData исчерпанная суточная квота приходит именно как 403
            # с текстом «Feature 'SUGGESTIONS' disabled for token», а не как 429.
            return DirectoryQuotaExceeded()
        if status == 429:
            return DirectoryQuotaExceeded("Слишком частые обращения к справочнику адресов")
        if status in _RETRYABLE_STATUSES:
            return DirectoryUnavailable()
        return DirectoryError()

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise DirectoryError("Справочник адресов вернул нечитаемый ответ") from None

    @staticmethod
    def _parse_suggestions(body: Any) -> tuple[DadataSuggestion, ...]:
        raw: Sequence[Mapping[str, Any]] = (body or {}).get("suggestions") or []
        parsed: list[DadataSuggestion] = []
        for item in raw:
            # Незнакомая или битая подсказка не должна ронять всю выдачу:
            # пользователь предпочтёт девять подсказок вместо ошибки формы.
            try:
                parsed.append(DadataSuggestion.model_validate(item))
            except ValueError:
                log.warning("dadata.unparsable_suggestion")
        return tuple(parsed)

    @staticmethod
    def _parse_party(suggestion: Mapping[str, Any]) -> PartyDraft | None:
        data = suggestion.get("data") or {}
        inn = data.get("inn")
        if not inn:
            return None

        name_block = data.get("name") or {}
        name = name_block.get("short_with_opf") or name_block.get("full_with_opf") or ""
        address_block = data.get("address") or {}
        address_data = address_block.get("data") or {}

        return PartyDraft(
            type="entrepreneur" if data.get("type") == "INDIVIDUAL" else "legal",
            name=name or suggestion.get("value", ""),
            inn=str(inn),
            kpp=data.get("kpp") or None,
            address=address_block.get("value") or None,
            city_fias_id=address_data.get("city_fias_id") or address_data.get("settlement_fias_id"),
            is_active=data.get("state", {}).get("status") == "ACTIVE",
        )
