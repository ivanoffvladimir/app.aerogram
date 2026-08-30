"""Rate shopping: параллельный опрос перевозчиков, ранжирование, сохранение.

Три требования ТЗ определяют устройство модуля целиком:

* **FR-1.3** — перевозчики опрашиваются параллельно, таймаут на одного
  3 секунды, общий дедлайн выдачи 5 секунд;
* **FR-1.4** — перевозчик, не ответивший в срок или вернувший ошибку,
  становится отдельной строкой выдачи с человекочитаемой причиной; ошибка
  одного не роняет выдачу;
* **FR-1.7** — каждый запрос и каждая котировка сохраняются, включая сырой
  ответ ТК.

К конкретным адаптерам модуль не обращается: только ``carriers.registry``
и DTO из ``carriers.base`` (контракт ``no-direct-carrier``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.carriers import registry
from aerogram.carriers.base import CarrierAccount as AdapterAccount
from aerogram.carriers.base import Party, Place, Quote, QuoteRequest
from aerogram.config import Settings
from aerogram.core.models import CarrierAccount
from aerogram.core.repository import CarrierAccountRepository
from aerogram.directories.repository import CarrierRepository
from aerogram.directories.service import CityMappingService
from aerogram.rating.models import RateQuote, RateRequest
from aerogram.rating.repository import RateRepository
from aerogram.rating.schemas import (
    RateErrorOut,
    RateQuoteOut,
    RateRequestIn,
    RateResponse,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.errors import AerogramError, CarrierError, CarrierTimeout
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money
from aerogram.shared.schemas import MoneySchema

__all__ = ["RateShoppingService", "rank_quotes"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _CarrierOutcome:
    """Итог опроса одного перевозчика."""

    carrier_code: str
    carrier_id: UUID
    account_id: UUID
    quotes: tuple[Quote, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class RateShoppingService:
    """Расчёт по подключённым перевозчикам."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._accounts = CarrierAccountRepository(session)
        self._carriers = CarrierRepository(session)
        self._mappings = CityMappingService(session)
        self._rates = RateRepository(session)

    async def quote(
        self, payload: RateRequestIn, *, tenant_id: UUID, user_id: UUID | None
    ) -> RateResponse:
        """Опросить перевозчиков и вернуть выдачу."""
        started = time.monotonic()

        accounts = await self._eligible_accounts(payload)
        outcomes = await self._poll(accounts, payload)
        duration_ms = int((time.monotonic() - started) * 1000)

        request = RateRequest(
            id=uuid7(),
            tenant_id=tenant_id,
            user_id=user_id,
            payload=payload.model_dump(mode="json"),
            hash=self._request_hash(payload),
            duration_ms=duration_ms,
            expires_at=utcnow() + timedelta(seconds=self._settings.quote_cache_ttl_seconds),
        )
        self._rates.add_request(request)

        rows = self._persist(request, outcomes, payload, tenant_id)
        await self._session.flush()

        quotes = [
            RateQuoteOut(
                rate_id=row.id,
                carrier=code,
                service_code=row.service_code,
                tariff_code=row.tariff_code,
                service_name=(row.raw_response or {}).get("service_name"),
                price=MoneySchema.of(Money(row.price_amount_minor or 0, row.currency)),
                price_source=row.price_source,
                transit_days_min=row.transit_days_min,
                transit_days_max=row.transit_days_max,
                promised_delivery_date=row.promised_delivery_date,
                meets_deadline=row.meets_deadline,
                rank=row.rank,
            )
            for row, code in rows
            if row.error_code is None
        ]
        errors = [
            RateErrorOut(
                carrier=outcome.carrier_code,
                code=outcome.error_code or "carrier_error",
                message=outcome.error_message or "Перевозчик не вернул расчёт",
            )
            for outcome in outcomes
            if outcome.error_code is not None
        ]

        log.info(
            "rating.completed",
            carriers=len(outcomes),
            quotes=len(quotes),
            errors=len(errors),
            duration_ms=duration_ms,
        )
        return RateResponse(
            request_id=request.id,
            expires_at=request.expires_at,
            duration_ms=duration_ms,
            quotes=quotes,
            errors=errors,
        )

    async def _eligible_accounts(self, payload: RateRequestIn) -> list[CarrierAccount]:
        """Активные учётные записи тенанта, отфильтрованные запросом.

        Пустой список ``carriers`` означает «все подключённые», а не «ни одного»:
        так расчёт из кабинета не требует перечислять перевозчиков руками.
        """
        accounts = await self._accounts.list_active()
        if not payload.carriers:
            return accounts

        wanted = set(payload.carriers)
        codes = {carrier.id: carrier.code for carrier in await self._carriers.list_active()}
        return [a for a in accounts if codes.get(a.carrier_id) in wanted]

    async def _poll(
        self, accounts: list[CarrierAccount], payload: RateRequestIn
    ) -> list[_CarrierOutcome]:
        """Опросить перевозчиков параллельно с общим дедлайном.

        Дедлайн общий, а не сумма таймаутов: пять перевозчиков по три секунды
        последовательно дали бы пятнадцать секунд ожидания вместо пяти (FR-1.3).
        """
        if not accounts:
            return []

        # Отсеиваем неподготовленные записи СРАЗУ и держим списки парой:
        # если фильтровать только задачи, индексы разъедутся, и строка
        # таймаута назовёт чужого перевозчика.
        prepared = [
            item
            for item in [await self._prepare(account, payload) for account in accounts]
            if item is not None
        ]
        if not prepared:
            return []

        tasks = [asyncio.create_task(self._ask_one(*item)) for item in prepared]

        done, pending = await asyncio.wait(tasks, timeout=self._settings.rating_deadline_seconds)
        for task in pending:
            task.cancel()

        outcomes = [task.result() for task in done]
        # Не успевшие в общий дедлайн — тоже строки выдачи, а не тишина.
        for item, task in zip(prepared, tasks, strict=True):
            if task in pending:
                account, carrier_code, carrier_id, _, _ = item
                outcomes.append(
                    _CarrierOutcome(
                        carrier_code=carrier_code,
                        carrier_id=carrier_id,
                        account_id=account.id,
                        error_code="carrier_timeout",
                        error_message="Перевозчик не ответил за отведённое время",
                    )
                )
        return outcomes

    async def _prepare(
        self, account: CarrierAccount, payload: RateRequestIn
    ) -> tuple[CarrierAccount, str, UUID, AdapterAccount, QuoteRequest] | None:
        """Собрать всё, что нужно адаптеру, до обращения к сети.

        Разрешение кодов городов и расшифровка учётных данных выполняются
        здесь: адаптер к базе не обращается (ADR-0005) и шифрование не знает.
        """
        carriers = await self._carriers.list_active()
        carrier = next((c for c in carriers if c.id == account.carrier_id), None)
        if carrier is None:
            return None

        try:
            credentials = self._decrypt(account)
        except Exception as exc:
            # Ловим широко намеренно: расшифровка бросает InvalidTag
            # из cryptography, JSONDecodeError, KeyError при отозванном ключе.
            # Нечитаемые данные ОДНОГО перевозчика не должны ронять расчёт
            # по остальным (FR-1.4). Текст исключения в лог не пишется:
            # он может содержать шифротекст.
            log.error(
                "rating.credentials_unreadable",
                carrier=carrier.code,
                error_type=type(exc).__name__,
            )
            return None

        adapter_account = AdapterAccount(
            account_id=str(account.id),
            carrier_code=carrier.code,
            mode=account.mode,  # type: ignore[arg-type]
            credentials=credentials,
            is_sandbox=account.is_sandbox,
            settings=dict(account.settings or {}),
        )

        sender = await self._party(payload.sender, account.carrier_id)
        recipient = await self._party(payload.recipient, account.carrier_id)

        request = QuoteRequest(
            sender=sender,
            recipient=recipient,
            places=tuple(
                Place(
                    weight_kg=place.weight_kg,
                    length_cm=place.length_cm,
                    width_cm=place.width_cm,
                    height_cm=place.height_cm,
                )
                for place in payload.places
            ),
            declared_value=payload.cargo.declared_value.to_money(),
            cargo_type=payload.cargo.type,
            pickup=payload.options.pickup,
            delivery_to_door=payload.options.delivery_to_door,
            insurance=payload.options.insurance,
            required_delivery_date=payload.required_delivery_date,
        )
        return account, carrier.code, carrier.id, adapter_account, request

    async def _party(self, party: object, carrier_id: UUID) -> Party:
        """Пункт с разрешённым кодом города перевозчика."""
        city_fias_id = getattr(party, "city_fias_id", None)
        carrier_city_code: str | None = None
        if city_fias_id:
            carrier_city_code, _ = await self._mappings.resolve_with_fallback(
                carrier_id, city_fias_id
            )
        return Party(
            city_fias_id=city_fias_id,
            city_name=str(getattr(party, "city_name", "")),
            carrier_city_code=carrier_city_code,
            postal_code=getattr(party, "postal_code", None),
            address=getattr(party, "address", None),
        )

    async def _ask_one(
        self,
        account: CarrierAccount,
        carrier_code: str,
        carrier_id: UUID,
        adapter_account: AdapterAccount,
        request: QuoteRequest,
    ) -> _CarrierOutcome:
        """Один перевозчик. Исключение наружу не выпускается."""
        try:
            adapter = registry.get_adapter(carrier_code)
        except LookupError:
            return _CarrierOutcome(
                carrier_code=carrier_code,
                carrier_id=carrier_id,
                account_id=account.id,
                error_code="carrier_not_available",
                error_message="Перевозчик не подключён к платформе",
            )

        try:
            quotes = await asyncio.wait_for(
                adapter.quote(request, adapter_account),
                timeout=self._settings.carrier_timeout_seconds,
            )
        except TimeoutError:
            error: AerogramError = CarrierTimeout(carrier_code=carrier_code)
        except CarrierError as exc:
            error = exc
        except Exception as exc:
            # Непредвиденный сбой адаптера — тоже строка выдачи, а не 500
            # на весь расчёт. Текст исключения наружу не отдаётся.
            log.error("rating.adapter_crashed", carrier=carrier_code, error_type=type(exc).__name__)
            error = CarrierError(carrier_code=carrier_code)
        else:
            return _CarrierOutcome(
                carrier_code=carrier_code,
                carrier_id=carrier_id,
                account_id=account.id,
                quotes=tuple(quotes),
            )

        return _CarrierOutcome(
            carrier_code=carrier_code,
            carrier_id=carrier_id,
            account_id=account.id,
            error_code=error.code,
            error_message=error.message_ru,
        )

    def _persist(
        self,
        request: RateRequest,
        outcomes: list[_CarrierOutcome],
        payload: RateRequestIn,
        tenant_id: UUID,
    ) -> list[tuple[RateQuote, str]]:
        """Сохранить котировки и строки ошибок."""
        rows: list[tuple[RateQuote, str]] = []

        for outcome in outcomes:
            if outcome.error_code is not None:
                rows.append(
                    (
                        RateQuote(
                            id=uuid7(),
                            tenant_id=tenant_id,
                            rate_request_id=request.id,
                            carrier_id=outcome.carrier_id,
                            carrier_account_id=outcome.account_id,
                            error_code=outcome.error_code,
                            error_message=outcome.error_message,
                            expires_at=request.expires_at,
                        ),
                        outcome.carrier_code,
                    )
                )
                continue

            for quote in outcome.quotes:
                meets = None
                if payload.required_delivery_date and quote.promised_delivery_date:
                    meets = quote.promised_delivery_date <= payload.required_delivery_date
                rows.append(
                    (
                        RateQuote(
                            id=uuid7(),
                            tenant_id=tenant_id,
                            rate_request_id=request.id,
                            carrier_id=outcome.carrier_id,
                            carrier_account_id=outcome.account_id,
                            service_code=quote.service_code,
                            tariff_code=quote.tariff_code,
                            price_amount_minor=quote.price.amount_minor,
                            currency=quote.price.currency,
                            price_source=quote.price_source,
                            transit_days_min=quote.transit_days_min,
                            transit_days_max=quote.transit_days_max,
                            promised_delivery_date=quote.promised_delivery_date,
                            meets_deadline=meets,
                            raw_response={**quote.raw, "service_name": quote.service_name},
                            expires_at=request.expires_at,
                        ),
                        outcome.carrier_code,
                    )
                )

        priced = [row for row, _ in rows if row.error_code is None]
        rank_quotes(priced, required_deadline=payload.required_delivery_date is not None)
        self._rates.add_quotes([row for row, _ in rows])
        return rows

    def _decrypt(self, account: CarrierAccount) -> dict[str, str]:
        """Расшифровать учётные данные перевозчика.

        Привязка к идентификатору записи: перенос шифротекста в чужую строку
        не расшифруется (см. ``shared.crypto``).
        """
        cipher = CredentialCipher(
            self._settings.credential_key_map, self._settings.credential_active_key_id
        )
        raw = cipher.decrypt(account.credentials_encrypted, aad=str(account.id).encode())
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()}

    @staticmethod
    def _request_hash(payload: RateRequestIn) -> str:
        """Отпечаток нормализованного запроса — ключ кэша выдачи (FR-1.6)."""
        canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rank_quotes(quotes: list[RateQuote], *, required_deadline: bool = False) -> None:
    """Проставить ранг строкам выдачи.

    Комбинированный ранг по умолчанию (FR-5.3): нормализованная цена 0,4,
    нормализованный срок 0,3, скор 0,3. Скора пока нет — до накопления
    статистики его вес не перераспределяется на цену, а просто не участвует:
    подставить вместо отсутствующего скора среднее значило бы выдать
    выдумку за данные (раздел 10.2 ТЗ).

    Строки, не укладывающиеся в требуемую дату, уходят вниз, но не скрываются
    (FR-5.4).
    """
    if not quotes:
        return

    prices = [q.price_amount_minor for q in quotes if q.price_amount_minor is not None]
    days = [q.transit_days_max for q in quotes if q.transit_days_max is not None]
    if not prices:
        return

    min_price, max_price = min(prices), max(prices)
    min_days, max_days = (min(days), max(days)) if days else (0, 0)

    def score(quote: RateQuote) -> tuple[int, float]:
        price_part = 0.0
        if quote.price_amount_minor is not None and max_price > min_price:
            price_part = (quote.price_amount_minor - min_price) / (max_price - min_price)
        transit_part = 0.0
        if quote.transit_days_max is not None and max_days > min_days:
            transit_part = (quote.transit_days_max - min_days) / (max_days - min_days)
        # Не уложившиеся в срок опускаются ниже всех уложившихся.
        misses_deadline = 1 if (required_deadline and quote.meets_deadline is False) else 0
        return misses_deadline, 0.4 * price_part + 0.3 * transit_part

    for position, quote in enumerate(sorted(quotes, key=score), start=1):
        quote.rank = position
