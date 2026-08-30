"""Rate shopping: параллельный опрос перевозчиков, нормализация, сохранение.

Устройство модуля задают три требования системного ТЗ, раздел 8:

* перевозчики опрашиваются параллельно, и общий срок ответа не зависит
  от самого медленного из них: таймаут на перевозчика и общий дедлайн;
* partial success — нормальное состояние: не ответивший перевозчик попадает
  в ``failures`` с причиной и не роняет выдачу остальных;
* каждый запрос и каждое предложение сохраняются вместе с сырым ответом ТК —
  это исходные данные Carrier Score и разбора спорных ситуаций.

Ранжирование здесь не делается: этим занимается ``routing`` на уже полученных
предложениях (ADR-0014). К конкретным адаптерам модуль не обращается — только
``carriers.registry`` и DTO из ``carriers.base``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.carriers import registry
from aerogram.carriers.base import CarrierAccount as AdapterAccount
from aerogram.carriers.base import Party, Place, Quote, QuoteRequest
from aerogram.config import Settings
from aerogram.core.models import CarrierAccount
from aerogram.core.repository import CarrierAccountRepository
from aerogram.core.service import decrypt_credentials
from aerogram.directories.dadata import DadataClient
from aerogram.directories.repository import CarrierRepository
from aerogram.directories.service import (
    CarrierPartyResolver,
    CityMappingService,
    CityService,
)
from aerogram.rating.models import RateOffer, RateQuote
from aerogram.rating.repository import RateRepository
from aerogram.rating.schemas import (
    CarrierFailureOut,
    RateOfferOut,
    RateRequestIn,
    RateResponse,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import IneligibilityReason, OfferSource, PriceSource
from aerogram.shared.errors import AerogramError, CarrierError, CarrierTimeout
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money
from aerogram.shared.schemas import AddressSchema, MoneySchema

__all__ = ["RateShoppingService", "rank_quotes"]

log = get_logger(__name__)

#: Ошибки, при которых повтор запроса имеет смысл. Ошибка авторизации
#: или валидации от повтора не исчезнет, и предлагать его — вводить в заблуждение.
RETRYABLE_FAILURES = frozenset({"carrier_timeout", "carrier_unavailable", "carrier_rate_limited"})


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

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        dadata: DadataClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._accounts = CarrierAccountRepository(session)
        self._carriers = CarrierRepository(session)
        self._mappings = CityMappingService(session)
        self._cities = CityService(session, dadata)
        self._parties = CarrierPartyResolver(self._cities, self._mappings)
        self._rates = RateRepository(session)

    async def quote(
        self, payload: RateRequestIn, *, tenant_id: UUID, user_id: UUID | None
    ) -> RateResponse:
        """Опросить перевозчиков и вернуть выдачу."""
        started = time.monotonic()

        # Город назначения разрешается один раз на запрос, а не на каждого
        # перевозчика: от него зависит таймзона, в которой обещанный день
        # превращается в момент.
        destination = await self._cities.resolve(
            payload.destination.city, payload.destination.region
        )
        destination_tz = destination.timezone if destination else None

        accounts = await self._eligible_accounts(payload)
        outcomes = await self._poll(accounts, payload)
        duration_ms = int((time.monotonic() - started) * 1000)

        quote = RateQuote(
            id=uuid7(),
            tenant_id=tenant_id,
            user_id=user_id,
            input_snapshot=payload.model_dump(mode="json"),
            hash=self._request_hash(payload),
            strategy=payload.strategy,
            deadline=payload.deadline,
            duration_ms=duration_ms,
            valid_until=utcnow() + timedelta(seconds=self._settings.quote_cache_ttl_seconds),
        )
        self._rates.add_quote(quote)

        rows = self._persist(quote, outcomes, payload, tenant_id, destination_tz)
        await self._session.flush()

        names = {c.id: c.name for c in await self._carriers.list_active()}

        offers = [
            RateOfferOut(
                id=row.id,
                carrier_id=row.carrier_id,
                carrier_name=names.get(row.carrier_id),
                service_code=row.service_code or "",
                service_name=(row.raw_response or {}).get("service_name"),
                source=row.source,
                total_cost=MoneySchema.of(Money(row.total_amount_minor or 0, row.currency)),
                eta=row.eta,
                deadline_margin_seconds=row.deadline_margin_seconds,
                lateness_seconds=row.lateness_seconds,
                on_time_probability=row.on_time_probability,
                probability_label=row.probability_label,
                risk=row.risk,
                confidence=row.score_confidence,
                eligible=row.eligible,
                ineligibility_reason=row.ineligibility_reason,
                valid_until=row.valid_until,
            )
            for row, _ in rows
            if row.error_code is None
        ]
        # Порядок выдачи задаётся здесь, иначе он достался бы от порядка
        # подключения перевозчиков — величины, к расчёту отношения не имеющей.
        # Это не ранжирование (ранжирует ``routing``, ADR-0014), а показ:
        # сначала валюта, чтобы рубли никогда не сравнивались с юанями числом,
        # затем сумма, затем имя и идентификатор — чтобы порядок был полным.
        offers.sort(
            key=lambda o: (
                o.total_cost.currency,
                o.total_cost.amount_minor,
                o.carrier_name or "",
                str(o.id),
            )
        )
        failures = [
            CarrierFailureOut(
                carrier_id=outcome.carrier_id,
                carrier_code=outcome.carrier_code,
                code=outcome.error_code or "carrier_error",
                message=outcome.error_message or "Перевозчик не вернул расчёт",
                retryable=outcome.error_code in RETRYABLE_FAILURES,
            )
            for outcome in outcomes
            if outcome.error_code is not None
        ]

        # Порядок отказов задаётся по той же причине, что и порядок
        # предложений: иначе он достался бы от порядка подключения
        # перевозчиков и переставлялся бы вместе с ним.
        # ``carrier_code`` необязателен в контракте, поэтому пустая строка:
        # без запасного значения сортировка упала бы на первом же отказе
        # перевозчика, которого не удалось опознать.
        failures.sort(key=lambda f: f.carrier_code or "")

        # «Никто не успевает» и «никто не ответил» — разные состояния, и путать
        # их нельзя: первое требует показать ближайшие альтернативы, второе —
        # разобраться с доступностью перевозчиков. Поэтому признак ставится
        # только когда предложения есть и ни одно из них не проходит по сроку.
        no_deadline_match = (
            bool(payload.deadline) and bool(offers) and not any(o.eligible for o in offers)
        )
        quote.no_deadline_match = no_deadline_match

        log.info(
            "rating.completed",
            carriers=len(outcomes),
            offers=len(offers),
            failures=len(failures),
            duration_ms=duration_ms,
        )
        return RateResponse(
            quote_id=quote.id,
            offers=offers,
            failures=failures,
            no_deadline_match=no_deadline_match,
            valid_until=quote.valid_until,
        )

    async def _eligible_accounts(self, payload: RateRequestIn) -> list[CarrierAccount]:
        """Активные учётные записи тенанта, отфильтрованные запросом.

        Пустой ``carrier_whitelist`` означает «все подключённые», а не «ни одного»:
        так расчёт из кабинета не требует перечислять перевозчиков руками.
        Чёрный список сильнее белого: перевозчик, попавший в оба, исключается —
        запрет должен побеждать разрешение, иначе запрет ничего не гарантирует.
        """
        accounts = await self._accounts.list_active()
        allowed = set(payload.carrier_whitelist)
        denied = set(payload.carrier_blacklist)
        if allowed:
            accounts = [a for a in accounts if a.carrier_id in allowed]
        if denied:
            accounts = [a for a in accounts if a.carrier_id not in denied]
        return accounts

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

        _, pending = await asyncio.wait(tasks, timeout=self._settings.rating_deadline_seconds)
        for task in pending:
            task.cancel()

        # Обходим задачи в порядке запуска, а не множество ``done``: обход
        # множества отдаёт результаты в произвольном порядке, и выдача
        # переставлялась от расчёта к расчёту без единого изменения данных.
        # Не успевшие в общий дедлайн — тоже строки выдачи, а не тишина.
        outcomes: list[_CarrierOutcome] = []
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
            else:
                outcomes.append(task.result())
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

        sender = await self._party(payload.origin, account.carrier_id)
        recipient = await self._party(payload.destination, account.carrier_id)

        request = QuoteRequest(
            sender=sender,
            recipient=recipient,
            places=tuple(
                Place(
                    weight_kg=package.weight_kg,
                    length_cm=_mm_to_cm(package.length_mm),
                    width_cm=_mm_to_cm(package.width_mm),
                    height_cm=_mm_to_cm(package.height_mm),
                )
                for package in payload.packages
            ),
            declared_value=payload.cargo_value.to_money(),
            cargo_type=payload.cargo_type,
            pickup=payload.pickup,
            delivery_to_door=payload.delivery_to_door,
            insurance=payload.insurance,
            required_delivery_date=payload.deadline.date() if payload.deadline else None,
        )
        return account, carrier.code, carrier.id, adapter_account, request

    async def _party(self, address: AddressSchema, carrier_id: UUID) -> Party:
        """Адрес из запроса → пункт с разрешённым кодом города перевозчика."""
        return await self._parties.party(address, carrier_id)

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
        quote: RateQuote,
        outcomes: list[_CarrierOutcome],
        payload: RateRequestIn,
        tenant_id: UUID,
        destination_tz: str | None,
    ) -> list[tuple[RateOffer, str]]:
        """Сохранить предложения и строки ошибок."""
        rows: list[tuple[RateOffer, str]] = []

        for outcome in outcomes:
            if outcome.error_code is not None:
                rows.append(
                    (
                        RateOffer(
                            id=uuid7(),
                            tenant_id=tenant_id,
                            quote_id=quote.id,
                            carrier_id=outcome.carrier_id,
                            carrier_account_id=outcome.account_id,
                            error_code=outcome.error_code,
                            error_message=outcome.error_message,
                            # Строка ошибки в рекомендации не участвует, но и не
                            # исчезает из выдачи: причина названа явно.
                            eligible=False,
                            ineligibility_reason=IneligibilityReason.SERVICE_UNAVAILABLE,
                            valid_until=quote.valid_until,
                        ),
                        outcome.carrier_code,
                    )
                )
                continue

            for offer in outcome.quotes:
                eta = _end_of_day(offer.promised_delivery_date, destination_tz)
                margin, lateness = _deadline_gap(eta, payload.deadline)
                meets = None if payload.deadline is None or eta is None else lateness == 0
                rows.append(
                    (
                        RateOffer(
                            id=uuid7(),
                            tenant_id=tenant_id,
                            quote_id=quote.id,
                            carrier_id=outcome.carrier_id,
                            carrier_account_id=outcome.account_id,
                            service_code=offer.service_code,
                            tariff_code=offer.tariff_code,
                            total_amount_minor=offer.price.amount_minor,
                            currency=offer.price.currency,
                            source=_offer_source(offer.price_source),
                            price_source=offer.price_source,
                            transit_days_min=offer.transit_days_min,
                            transit_days_max=offer.transit_days_max,
                            promised_delivery_date=offer.promised_delivery_date,
                            eta=eta,
                            deadline_margin_seconds=margin,
                            lateness_seconds=lateness,
                            meets_deadline=meets,
                            # Не уложившиеся в срок не скрываются, а помечаются
                            # причиной и уходят вниз (продуктовое ТЗ, раздел 7).
                            eligible=meets is not False,
                            ineligibility_reason=(
                                IneligibilityReason.MISSES_DEADLINE if meets is False else None
                            ),
                            raw_response={**offer.raw, "service_name": offer.service_name},
                            valid_until=quote.valid_until,
                        ),
                        outcome.carrier_code,
                    )
                )

        priced = [row for row, _ in rows if row.error_code is None]
        rank_quotes(priced, required_deadline=payload.deadline is not None)
        self._rates.add_offers([row for row, _ in rows])
        return rows

    def _decrypt(self, account: CarrierAccount) -> dict[str, str]:
        """Расшифровать учётные данные перевозчика."""
        return decrypt_credentials(account, self._settings)

    @staticmethod
    def _request_hash(payload: RateRequestIn) -> str:
        """Отпечаток нормализованного запроса — ключ кэша выдачи (FR-1.6)."""
        canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _end_of_day(day: date | None, timezone_name: str | None) -> datetime | None:
    """Обещанный день → момент, к которому доставка обещана.

    Перевозчик обещает день, а дедлайн задаётся моментом, и сравнивать их
    напрямую нельзя. Берётся конец дня — самый поздний момент, совместимый
    с обещанием: взять начало дня значило бы обещать за перевозчика больше,
    чем он сказал.

    Таймзона — города назначения: конец дня во Владивостоке наступает
    на десять часов раньше московского, и в дедлайн по Москве такая доставка
    укладывается, хотя по UTC выглядела бы опоздавшей.
    """
    if day is None:
        return None
    try:
        tz = ZoneInfo(timezone_name) if timezone_name else UTC
    except ZoneInfoNotFoundError:
        log.warning("rating.unknown_timezone", timezone=timezone_name)
        tz = UTC
    return datetime.combine(day, datetime.max.time(), tzinfo=tz)


def _deadline_gap(eta: datetime | None, deadline: datetime | None) -> tuple[int | None, int | None]:
    """Запас до дедлайна и величина опоздания, в секундах.

    Обе величины неотрицательны и взаимоисключающи: либо запас, либо опоздание.
    Отрицательный запас читался бы двусмысленно.
    """
    if eta is None or deadline is None:
        return None, None
    gap = int((deadline - eta).total_seconds())
    return (gap, 0) if gap >= 0 else (0, -gap)


def _mm_to_cm(value: int | None) -> int:
    """Миллиметры контракта → сантиметры адаптеров, вверх до целого.

    Округление вниз занизило бы объёмный вес и, значит, цену: 305 мм это 31 см
    для тарифа, а не 30. Отсутствующий габарит даёт 1 см, а не ноль: нулевой
    габарит запрещён проверкой объёмного веса.
    """
    if value is None:
        return 1
    return max(1, -(-value // 10))


def _offer_source(price_source: PriceSource | None) -> OfferSource | None:
    """Внутренний источник цены → значение контракта (``RateOffer.source``).

    Публичный тариф ПЭК ни в одно из двух значений контракта не укладывается —
    расхождение вынесено в docs/status.md и здесь даёт None, а не выдумку.
    """
    if price_source is PriceSource.OWN_CONTRACT:
        return OfferSource.CLIENT_CONTRACT
    if price_source is PriceSource.AEROGRAM:
        return OfferSource.LOGISTICS_OS
    return None


def rank_quotes(quotes: list[RateOffer], *, required_deadline: bool = False) -> None:
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

    prices = [q.total_amount_minor for q in quotes if q.total_amount_minor is not None]
    days = [q.transit_days_max for q in quotes if q.transit_days_max is not None]
    if not prices:
        return

    min_price, max_price = min(prices), max(prices)
    min_days, max_days = (min(days), max(days)) if days else (0, 0)

    def score(quote: RateOffer) -> tuple[int, float]:
        price_part = 0.0
        if quote.total_amount_minor is not None and max_price > min_price:
            price_part = (quote.total_amount_minor - min_price) / (max_price - min_price)
        transit_part = 0.0
        if quote.transit_days_max is not None and max_days > min_days:
            transit_part = (quote.transit_days_max - min_days) / (max_days - min_days)
        # Не уложившиеся в срок опускаются ниже всех уложившихся.
        misses_deadline = 1 if (required_deadline and quote.meets_deadline is False) else 0
        return misses_deadline, 0.4 * price_part + 0.3 * transit_part

    for position, quote in enumerate(sorted(quotes, key=score), start=1):
        quote.rank = position
