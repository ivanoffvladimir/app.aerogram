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
from aerogram.directories.models import City
from aerogram.directories.repository import CarrierRepository
from aerogram.directories.service import (
    CarrierPartyResolver,
    CityMappingService,
    CityService,
)
from aerogram.intelligence.models import CarrierScoreSnapshot
from aerogram.intelligence.repository import ScoreRepository
from aerogram.rating.models import CostComponent, RateOffer, RateQuote
from aerogram.rating.repository import RateRepository
from aerogram.rating.schemas import (
    BlockedCarrierOut,
    CarrierFailureOut,
    CostComponentOut,
    RateOfferOut,
    RateRequestIn,
    RateResponse,
)
from aerogram.routing.repository import RoutingRepository
from aerogram.routing.rules import (
    Policy,
    RequestFacts,
    evaluate,
    parse_rules,
    policy_fingerprint,
)
from aerogram.routing.snapshot import dump_policy_snapshot
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import (
    CostComponentType,
    IneligibilityReason,
    OfferSource,
    PriceSource,
    ScoreConfidence,
)
from aerogram.shared.errors import AerogramError, CarrierError, CarrierTimeout
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money, chargeable_weight, mm_to_cm
from aerogram.shared.schemas import AddressSchema, MoneySchema, PackageSchema

__all__ = ["RateShoppingService", "rank_quotes"]

log = get_logger(__name__)

#: Ошибки, при которых повтор запроса имеет смысл. Ошибка авторизации
#: или валидации от повтора не исчезнет, и предлагать его — вводить в заблуждение.
RETRYABLE_FAILURES = frozenset({"carrier_timeout", "carrier_unavailable", "carrier_rate_limited"})

#: Код строки «запрещено политикой». Строка хранится среди предложений,
#: потому что ограничение таблицы требует у строки либо цену, либо код
#: ошибки, а цены здесь нет и не должно быть: перевозчика не спрашивали.
#: В ответе она уходит не в ``failures``, а в отдельный список: «не вернул
#: расчёт» здесь неправда.
BLOCKED_BY_POLICY = "blocked_by_policy"


@dataclass(frozen=True, slots=True)
class _CarrierOutcome:
    """Итог опроса одного перевозчика."""

    carrier_code: str
    carrier_id: UUID
    account_id: UUID
    quotes: tuple[Quote, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _BlockedCarrier:
    """Перевозчик, которого не спросили: правило маршрутизации запретило."""

    carrier_code: str
    carrier_id: UUID
    account_id: UUID
    reason: IneligibilityReason
    message: str


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
        self._scores = ScoreRepository(session)
        self._routing = RoutingRepository(session)

    async def quote(
        self, payload: RateRequestIn, *, tenant_id: UUID, user_id: UUID | None
    ) -> RateResponse:
        """Опросить перевозчиков и вернуть выдачу.

        Тот же запрос в пределах срока жизни выдачи не опрашивает перевозчиков
        заново, а возвращает уже полученную (FR-1.6).
        """
        started = time.monotonic()
        rules = parse_rules(list(await self._routing.active_rules()))
        # Версия политики входит в отпечаток запроса: иначе изменение правил
        # не отменяло бы уже снятую выдачу, и пятнадцать минут после запрета
        # запрещённый перевозчик продолжал бы показываться с ценой.
        policy_version = policy_fingerprint(rules)
        request_hash = self._request_hash(payload, policy_version)
        accounts = await self._eligible_accounts(payload)

        reused = await self._reusable(request_hash, accounts)
        if reused is not None:
            log.info(
                "rating.reused",
                quote_id=str(reused.id),
                age_seconds=int((utcnow() - reused.created_at).total_seconds()),
            )
            return await self._response(reused, list(reused.offers))

        # Города разрешаются один раз на запрос, а не на каждого перевозчика.
        # От назначения зависит таймзона, в которой обещанный день превращается
        # в момент; оба нужны правилам маршрутизации. Делается это после
        # проверки на повтор: готовой выдаче разрешение города уже не нужно.
        origin = await self._cities.resolve(payload.origin.city, payload.origin.region)
        destination = await self._cities.resolve(
            payload.destination.city, payload.destination.region
        )
        destination_tz = destination.timezone if destination else None

        # Правила применяются ДО опроса: все их условия — свойства запроса,
        # а не предложения (ADR-0028). Запрещённого перевозчика не спрашивают:
        # вызов стоит денег и времени, а у Почты России ещё и суточной квоты.
        # Правилам подаются коды ТОЛЬКО подключённых тенантом перевозчиков.
        # Весь справочник платформы дал бы вердикты про тех, с кем у клиента
        # нет договора, — то есть политика рассуждала бы о перевозчиках,
        # которых в этой выдаче быть не может ни при каком правиле.
        codes = await self._codes()
        # Факты держатся переменной: они нужны дважды — правилам сейчас
        # и снимку расчёта дальше. Восстановить их позже нельзя (ADR-0029).
        facts = _facts(payload, _fias(origin), _fias(destination))
        policy = evaluate(
            rules,
            facts,
            sorted({codes[a.carrier_id] for a in accounts if a.carrier_id in codes}),
        )
        allowed, blocked = self._apply_policy(accounts, policy, codes)

        outcomes = await self._poll(allowed, payload, insurance=policy.require_insurance)
        duration_ms = int((time.monotonic() - started) * 1000)

        quote = RateQuote(
            id=uuid7(),
            tenant_id=tenant_id,
            user_id=user_id,
            input_snapshot=payload.model_dump(mode="json"),
            hash=request_hash,
            strategy=payload.strategy,
            deadline=payload.deadline,
            duration_ms=duration_ms,
            # Вердикт политики замораживается здесь и только здесь: правила
            # применяются при расчёте, а решение принимается позже и своих
            # фактов уже не имеет (ADR-0029).
            policy_version=policy_version,
            policy_snapshot=dump_policy_snapshot(facts, policy),
            valid_until=utcnow() + timedelta(seconds=self._settings.quote_cache_ttl_seconds),
        )
        self._rates.add_quote(quote)

        # Скор снимается в момент расчёта и остаётся в предложении навсегда:
        # рекомендация объясняется теми числами, которые были видны тогда,
        # а не теми, что получились после следующего пересчёта (FR-7.6).
        scores = await self._scores.latest_by_carrier()
        rows = self._persist(
            quote, outcomes, payload, tenant_id, destination_tz, scores, blocked=blocked
        )
        await self._session.flush()

        # ``carrier_code`` берётся из опроса, а не из справочника: перевозчика
        # могли отключить между расчётом и ответом, а отказ обязан назвать того,
        # кто отказал.
        response = await self._response(
            quote,
            [row for row, _ in rows],
            codes={outcome.carrier_id: outcome.carrier_code for outcome in outcomes}
            | {item.carrier_id: item.carrier_code for item in blocked},
        )
        quote.no_deadline_match = response.no_deadline_match

        log.info(
            "rating.completed",
            carriers=len(outcomes),
            offers=len(response.offers),
            failures=len(response.failures),
            blocked=len(response.blocked),
            policy_version=policy_version,
            duration_ms=duration_ms,
        )
        return response

    async def _reusable(
        self, request_hash: str, accounts: list[CarrierAccount]
    ) -> RateQuote | None:
        """Выдача, которую можно вернуть вместо нового опроса (FR-1.6).

        Возвращается не всякая живая выдача с тем же отпечатком.

        Пустая — никогда: расчёт, в котором не ответил никто, означает сбой,
        и отдавать его следующие пятнадцать минут значит растянуть минутную
        недоступность перевозчиков на четверть часа.

        И только та, что снята при том же наборе подключённых учётных записей.
        Иначе перевозчик, подключённый минуту назад, не появлялся бы в выдаче
        до конца срока жизни предыдущей, а отключённый — продолжал бы в ней
        показываться.

        Сравнение неточно в одну сторону: перевозчик, ответивший без ошибки,
        но без единого тарифа, строк не оставляет, и следующий такой же запрос
        будет посчитан заново. Это лишний расчёт, а не неверный ответ; точное
        сравнение требует хранить состав опроса в самой выдаче, то есть
        изменения схемы, а это решение человека (CLAUDE.md §7).
        """
        quote = await self._rates.find_reusable(request_hash, utcnow())
        if quote is None:
            return None
        priced = [offer for offer in quote.offers if offer.error_code is None]
        if not priced:
            return None
        if {offer.carrier_account_id for offer in quote.offers} != {a.id for a in accounts}:
            return None
        return quote

    async def _response(
        self,
        quote: RateQuote,
        offers: list[RateOffer],
        codes: dict[UUID, str] | None = None,
    ) -> RateResponse:
        """Собрать ответ по снимку выдачи.

        Сборка одна на оба пути — свежий расчёт и повтор. Разойдись они,
        повтор отличался бы от первого ответа, а FR-1.6 обещает ровно
        обратное.
        """
        carriers = await self._carriers.list_active()
        names = {c.id: c.name for c in carriers}
        carrier_codes = {c.id: c.code for c in carriers} | (codes or {})

        priced = [
            RateOfferOut(
                id=row.id,
                carrier_id=row.carrier_id,
                carrier_name=names.get(row.carrier_id),
                service_code=row.service_code or "",
                service_name=(row.raw_response or {}).get("service_name"),
                source=row.source,
                # Порядок задаётся здесь, а не выборкой: без него строки
                # пришли бы как попало и расшифровка переставлялась бы
                # от показа к показу. Убывание суммы, затем подпись —
                # чтобы порядок был полным даже у одинаковых сумм.
                cost_components=[
                    CostComponentOut(
                        type=component.type,
                        money=MoneySchema.of(Money(component.amount_minor, component.currency)),
                        rate_percent=component.rate_percent,
                        description=component.description,
                    )
                    for component in sorted(
                        row.cost_components,
                        key=lambda c: (-c.amount_minor, c.description or ""),
                    )
                ],
                total_cost=MoneySchema.of(Money(row.total_amount_minor or 0, row.currency)),
                eta=row.eta,
                deadline_margin_seconds=row.deadline_margin_seconds,
                lateness_seconds=row.lateness_seconds,
                on_time_probability=row.on_time_probability,
                probability_label=row.probability_label,
                carrier_score=row.score_at_quote,
                risk=row.risk,
                confidence=_shown_confidence(row.score_confidence),
                eligible=row.eligible,
                ineligibility_reason=row.ineligibility_reason,
                valid_until=row.valid_until,
            )
            for row in offers
            if row.error_code is None
        ]
        # Порядок выдачи задаётся здесь, иначе он достался бы от порядка
        # подключения перевозчиков — величины, к расчёту отношения не имеющей.
        # Это не ранжирование (ранжирует ``routing``, ADR-0014), а показ:
        # сначала валюта, чтобы рубли никогда не сравнивались с юанями числом,
        # затем сумма, затем имя и идентификатор — чтобы порядок был полным.
        priced.sort(
            key=lambda o: (
                o.total_cost.currency,
                o.total_cost.amount_minor,
                o.carrier_name or "",
                str(o.id),
            )
        )
        # Запрет и отказ разводятся здесь, а не смешиваются: «перевозчик
        # не вернул расчёт» про запрещённого — неправда, его не спрашивали.
        blocked = [
            BlockedCarrierOut(
                carrier_id=row.carrier_id,
                carrier_code=carrier_codes.get(row.carrier_id),
                carrier_name=names.get(row.carrier_id),
                reason=row.ineligibility_reason or IneligibilityReason.TENANT_POLICY,
                message=row.error_message or "Запрещено политикой компании",
            )
            for row in offers
            if row.error_code == BLOCKED_BY_POLICY
        ]
        blocked.sort(key=lambda b: b.carrier_code or "")

        failures = [
            CarrierFailureOut(
                carrier_id=row.carrier_id,
                carrier_code=carrier_codes.get(row.carrier_id),
                code=row.error_code or "carrier_error",
                message=row.error_message or "Перевозчик не вернул расчёт",
                retryable=row.error_code in RETRYABLE_FAILURES,
            )
            for row in offers
            if row.error_code is not None and row.error_code != BLOCKED_BY_POLICY
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
            bool(quote.deadline) and bool(priced) and not any(o.eligible for o in priced)
        )
        return RateResponse(
            quote_id=quote.id,
            offers=priced,
            failures=failures,
            blocked=blocked,
            no_deadline_match=no_deadline_match,
            valid_until=quote.valid_until,
        )

    async def _eligible_accounts(self, payload: RateRequestIn) -> list[CarrierAccount]:
        """Активные учётные записи тенанта, отфильтрованные запросом.

        Пустой ``carrier_whitelist`` означает «все подключённые», а не «ни одного»:
        так расчёт из кабинета не требует перечислять перевозчиков руками.
        Чёрный список сильнее белого: перевозчик, попавший в оба, исключается —
        запрет должен побеждать разрешение, иначе запрет ничего не гарантирует.

        Список на конкретную отправку только **сужает**. Правила маршрутизации
        применяются после этого отбора и сужают его дальше, поэтому список
        в теле запроса не может расширить то, что разрешила политика. Иначе
        жёсткий запрет обходился бы передачей списка в запросе — то есть это
        было бы не удобство, а дыра: политику отменял бы тот, кого она
        ограничивает (ADR-0028).
        """
        accounts = await self._accounts.list_active()
        allowed = set(payload.carrier_whitelist)
        denied = set(payload.carrier_blacklist)
        if allowed:
            accounts = [a for a in accounts if a.carrier_id in allowed]
        if denied:
            accounts = [a for a in accounts if a.carrier_id not in denied]
        return accounts

    async def _codes(self) -> dict[UUID, str]:
        """Идентификатор перевозчика → его код. Правила пишутся кодами."""
        return {carrier.id: carrier.code for carrier in await self._carriers.list_active()}

    def _apply_policy(
        self, accounts: list[CarrierAccount], policy: Policy, codes: dict[UUID, str]
    ) -> tuple[list[CarrierAccount], list[_BlockedCarrier]]:
        """Разделить учётные записи на «спросим» и «запрещено правилом».

        Учётная запись перевозчика, которого нет в справочнике активных,
        не спрашивается и в запрещённые не попадает: правила про неё ничего
        не решали, и объявлять её запрещённой значило бы приписать политике
        чужое решение.
        """
        allowed: list[CarrierAccount] = []
        blocked: list[_BlockedCarrier] = []

        for account in accounts:
            code = codes.get(account.carrier_id)
            if code is None:
                continue
            verdict = policy.verdict(code)
            if verdict is not None and not verdict.allowed:
                blocked.append(
                    _BlockedCarrier(
                        carrier_code=code,
                        carrier_id=account.carrier_id,
                        account_id=account.id,
                        reason=verdict.reason or IneligibilityReason.TENANT_POLICY,
                        message=_blocked_message(verdict.reason, verdict.rule_name),
                    )
                )
                continue

            # Обещать страховку тому, у кого её нет, хуже, чем не показать
            # вариант: до страхового случая разница не видна, а после неё
            # поздно (ADR-0028).
            if policy.require_insurance and not _supports_insurance(code):
                blocked.append(
                    _BlockedCarrier(
                        carrier_code=code,
                        carrier_id=account.carrier_id,
                        account_id=account.id,
                        reason=IneligibilityReason.TENANT_POLICY,
                        message=(
                            f"Правило «{policy.insurance_rule}» требует страхования, "
                            "а перевозчик его не поддерживает"
                        ),
                    )
                )
                continue

            allowed.append(account)

        return allowed, blocked

    async def _poll(
        self, accounts: list[CarrierAccount], payload: RateRequestIn, *, insurance: bool = False
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
            for item in [
                await self._prepare(account, payload, insurance=insurance) for account in accounts
            ]
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
        self, account: CarrierAccount, payload: RateRequestIn, *, insurance: bool = False
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

        # Считает ли перевозчик объёмный вес сам. Незарегистрированный адаптер
        # до сети всё равно не дойдёт (``_ask_one`` вернёт строку отказа),
        # поэтому здесь достаточно не подменять вес.
        try:
            computes_volumetric = registry.get_adapter(
                carrier.code
            ).capabilities.computes_volumetric_weight
        except LookupError:
            computes_volumetric = True

        sender = await self._party(payload.origin, account.carrier_id)
        recipient = await self._party(payload.destination, account.carrier_id)

        request = QuoteRequest(
            sender=sender,
            recipient=recipient,
            places=tuple(
                _place(package, carrier.volumetric_divisor, computes_volumetric)
                for package in payload.packages
            ),
            declared_value=payload.cargo_value.to_money(),
            cargo_type=payload.cargo_type,
            pickup=payload.pickup,
            delivery_to_door=payload.delivery_to_door,
            # Обязательное страхование входит в цену, поэтому решается ДО
            # опроса: посчитать без него и добавить потом значило бы показать
            # оператору сумму, которой не будет в счёте.
            insurance=payload.insurance or insurance,
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
        scores: dict[UUID, CarrierScoreSnapshot],
        *,
        blocked: list[_BlockedCarrier],
    ) -> list[tuple[RateOffer, str]]:
        """Сохранить предложения, строки ошибок и строки запретов."""
        rows: list[tuple[RateOffer, str]] = []

        # Запрещённые сохраняются вместе с остальными, а не собираются заново
        # при ответе: повтор выдачи (FR-1.6) читает те же строки, и запрет,
        # существующий только в памяти, из повтора бы исчез.
        for item in blocked:
            rows.append(
                (
                    RateOffer(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        quote_id=quote.id,
                        carrier_id=item.carrier_id,
                        carrier_account_id=item.account_id,
                        error_code=BLOCKED_BY_POLICY,
                        error_message=item.message,
                        eligible=False,
                        ineligibility_reason=item.reason,
                        valid_until=quote.valid_until,
                    ),
                    item.carrier_code,
                )
            )

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

            score = scores.get(outcome.carrier_id)
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
                            # Скор пишется и тогда, когда его нет: «смотрели,
                            # данных не хватило» и «не смотрели» — разные
                            # состояния, и через месяц их не различить.
                            score_at_quote=score.score if score else None,
                            score_confidence=score.confidence if score else None,
                            score_scope=score.scope_type if score else None,
                            valid_until=quote.valid_until,
                            cost_components=_components(offer, tenant_id),
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
    def _request_hash(payload: RateRequestIn, policy_version: str) -> str:
        """Отпечаток нормализованного запроса — ключ повторного использования
        выдачи (FR-1.6).

        Нормализация здесь — не украшение: без неё два запроса, отличающиеся
        только записью, считались бы разными, и выдача не переиспользовалась
        бы почти никогда.

        Приводится два вида различий. Моменты времени — к UTC: «12:00+03:00»
        и «09:00Z» это один момент, а в JSON это разные строки. Списки услуг
        и списки перевозчиков — к отсортированному виду: по смыслу это
        множества, и порядок в них ничего не значит.

        ``packages`` намеренно не сортируются. Их порядок отражает порядок
        мест в отправлении, и он попадает в этикетки и опись, а не только
        в расчёт: сортировать их значило бы объявить одинаковыми запросы,
        по которым получатся разные документы.
        """
        data = payload.model_dump(mode="json")
        # Версия политики — часть запроса, хотя клиент её не присылает:
        # при одном и том же теле разные правила дают разную выдачу,
        # и общий отпечаток вернул бы вчерашний состав перевозчиков.
        data["policy_version"] = policy_version
        for field in ("ship_at", "deadline"):
            value = getattr(payload, field)
            data[field] = None if value is None else value.astimezone(UTC).isoformat()
        for field in ("additional_services", "carrier_whitelist", "carrier_blacklist"):
            data[field] = sorted(data[field])
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _shown_confidence(stored: str | None) -> ScoreConfidence | None:
    """Доверие к скору так, как его допускает контракт.

    В снимке хранится и ``insufficient`` — это осмысленное значение: смотрели,
    данных не хватило. В схеме ``RateOffer`` его нет, там перечислены только
    low, medium, high и null. И это правильно: раз числа нет, говорить
    о доверии к нему нечего, а выдумывать шестое значение в ответе значило бы
    разойтись с контрактом ради слова, которое клиенту ничего не добавляет.
    """
    if stored is None or stored == ScoreConfidence.INSUFFICIENT:
        return None
    return ScoreConfidence(stored)


#: Предел длины подписи составляющей. Подпись приходит от перевозчика —
#: у ПЭК это вообще свободный текст поля ``info``, — и попадает на экран.
#: Длина строки не то, ради чего существует расшифровка.
_COMPONENT_LABEL_LIMIT = 200


def _components(quote: Quote, tenant_id: UUID) -> list[CostComponent]:
    """Расшифровка цены перевозчика → строки ``cost_components``.

    Её считают три адаптера, и до этой функции она не доходила никуда:
    ``CostComponent`` не создавался ни одной строкой кода, поэтому поле
    контракта ``RateOffer.cost_components`` всегда было пустым списком.
    Вместе с ним терялась и надбавка за негабарит, которую Почта России
    называет прямо и уже включает в итог.

    **Тип у всех строк — ``other``, а правду несёт подпись.** Ключи
    расшифровки разнородны: у Почты это её собственные названия, у Деловых
    Линий наши (`pickup`, `delivery`, `insurance`), у ПЭК свободный русский
    текст самого перевозчика. Классифицировать их здесь значило бы искать
    подстроки по-русски в домене, то есть завести знание о перевозчиках
    там, где его быть не должно (ADR-0005). Честный способ — чтобы тип
    называл адаптер, а это правка ``carriers/base.py``, то есть построчное
    ревью человека (CLAUDE.md §7). До тех пор ``other`` честнее выдуманного
    типа: он говорит «мы не знаем», а не называет наугад.

    **Порядок строк задаётся суммой, а не порядком перевозчика.** Первая
    редакция сортировала по идентификатору, считая UUIDv7 сортируемым по
    времени создания. Внутри одной миллисекунды это неверно: в ``shared.ids``
    счётчика нет, и младшие биты случайны, — а все строки одного предложения
    создаются именно в одну миллисекунду. Расшифровка переставлялась бы
    от показа к показу без единого изменения данных. Поймано тестом.

    Порядок перевозчика при этом ничего не значит для читающего: у Почты
    это порядок нашего же ``RATE_FIELDS``, у ПЭК — обход дерева услуг.
    А убывание суммы ставит наверх то, из чего цена в основном и состоит.
    """
    rows: list[CostComponent] = []
    for label, amount in quote.price_breakdown.items():
        if amount.currency != quote.price.currency:
            # Строка в чужой валюте не сохраняется вовсе. Сложить её с ценой
            # нельзя (CLAUDE.md §6), а показать рядом — значит показать
            # расшифровку, которая не сходится с итогом.
            log.warning(
                "rating.component_currency_mismatch",
                component=amount.currency,
                offer=quote.price.currency,
            )
            continue
        if amount.amount_minor == 0:
            # Нулевая строка не просто шум: «Надбавка за негабарит — 0 ₽»
            # читается как «надбавка есть», хотя её нет.
            continue
        rows.append(
            CostComponent(
                id=uuid7(),
                tenant_id=tenant_id,
                type=CostComponentType.OTHER,
                amount_minor=amount.amount_minor,
                currency=amount.currency,
                description=str(label)[:_COMPONENT_LABEL_LIMIT],
            )
        )
    return rows


def _fias(city: City | None) -> UUID | None:
    """Идентификатор ФИАС города как UUID — тем, кто сравнивает его с правилом.

    ``fias_id`` хранится строкой, а условие правила разбирается в ``UUID``:
    так «0c5b2444-70A0-…» и «0c5b2444-70a0-…» не оказываются разными городами.
    Нечитаемый идентификатор даёт ``None``, то есть «город не разрешён»: это
    поломка справочника, а не запроса, и угадывать здесь нечего.
    """
    if city is None:
        return None
    try:
        return UUID(city.fias_id)
    except (ValueError, AttributeError, TypeError):
        log.warning("rating.fias_id_unreadable", city_id=str(city.id))
        return None


def _facts(payload: RateRequestIn, origin: UUID | None, destination: UUID | None) -> RequestFacts:
    """Свойства запроса, по которым правила узнают «свой» случай."""
    return RequestFacts(
        origin_fias_id=origin,
        destination_fias_id=destination,
        billable_weight_grams=_policy_weight_grams(payload.packages),
        cargo_value=payload.cargo_value.to_money(),
        cargo_type=payload.cargo_type,
        dangerous=payload.dangerous,
    )


def _policy_weight_grams(packages: list[PackageSchema]) -> int:
    """Расчётный вес всего отправления для правил, в граммах.

    Делитель объёмного веса берётся ПЛАТФОРМЕННЫЙ, а не перевозчика. Правило
    одно на всех, и порог «тяжелее 30 кг» не может означать у разных
    перевозчиков разный вес — иначе правило срабатывало бы на части выдачи,
    и объяснить это оператору было бы нечем.

    Значение по умолчанию выбрано в сторону осторожности (меньший делитель
    даёт больший объёмный вес), то есть правило скорее сработает, чем нет:
    ошибаться здесь следует в сторону запрета.
    """
    total = sum(
        chargeable_weight(
            package.weight_kg,
            mm_to_cm(package.length_mm),
            mm_to_cm(package.width_mm),
            mm_to_cm(package.height_mm),
        )
        for package in packages
    )
    return int(total * 1000)


def _supports_insurance(carrier_code: str) -> bool:
    """Умеет ли перевозчик страховать. Неизвестный адаптер — считаем, что нет.

    Неподключённый перевозчик всё равно не доедет до сети, и «умеет» о нём
    было бы обещанием, которое некому исполнить.
    """
    try:
        return registry.get_adapter(carrier_code).capabilities.supports_insurance
    except LookupError:
        return False


#: Фразы запрета для оператора. Причина машинная, а строка — та, которую
#: человек прочитает в выдаче, поэтому она называет правило по имени: без
#: имени у логиста нет ни одного способа выяснить, каким именно запрещено.
_BLOCKED_MESSAGES = {
    IneligibilityReason.NOT_IN_WHITELIST: "Не входит в список, разрешённый правилом «{rule}»",
    IneligibilityReason.CARGO_RESTRICTED: "Правило «{rule}»: перевозчику нельзя такой груз",
}
_BLOCKED_DEFAULT = "Запрещено правилом «{rule}»"


def _blocked_message(reason: IneligibilityReason | None, rule_name: str | None) -> str:
    if rule_name is None:
        return "Запрещено политикой компании"
    template = _BLOCKED_MESSAGES.get(reason or IneligibilityReason.TENANT_POLICY, _BLOCKED_DEFAULT)
    return template.format(rule=rule_name)


def _place(package: PackageSchema, divisor: int, carrier_computes: bool) -> Place:
    """Грузовое место в терминах адаптера, с весом, по которому платят.

    Перевозчик тарифицирует по большему из двух весов: фактическому
    и объёмному, Д × Ш × В (см) / делитель (FR-1.2). Делитель у каждого свой
    и лежит в справочнике перевозчиков; по умолчанию 5000.

    Подмена делается ТОЛЬКО для перевозчика, который объёмный вес не считает
    сам. Иначе получился бы двойной учёт: он посчитал бы объёмный вес по нашим
    габаритам ещё раз, уже поверх подменённого веса.

    Габариты необязательны в контракте. Отсутствующий габарит ``mm_to_cm``
    отдаёт как 1 см, поэтому объёмный вес такого места ничтожен и максимум
    остаётся за фактическим — то есть отсутствие габаритов никогда
    не завышает цену.
    """
    length_cm = mm_to_cm(package.length_mm)
    width_cm = mm_to_cm(package.width_mm)
    height_cm = mm_to_cm(package.height_mm)
    weight_kg = (
        package.weight_kg
        if carrier_computes
        else chargeable_weight(package.weight_kg, length_cm, width_cm, height_cm, divisor)
    )
    return Place(
        weight_kg=weight_kg,
        length_cm=length_cm,
        width_cm=width_cm,
        height_cm=height_cm,
    )


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
