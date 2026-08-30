"""Создание, отмена и сверка отправлений.

Главное свойство модуля — **отправлений-«призраков» не бывает** (FR-2.5).
Призрак появляется так: мы просим перевозчика создать заказ, он создаёт,
а ответ до нас не доходит. У клиента списаны деньги и поехал груз, а в системе
нет ни строки. Найти такой заказ потом можно только вручную, и только если
кто-то заметил.

Защита состоит из трёх частей, и ни одна не работает без остальных:

1. **Черновик коммитится своей транзакцией до вызова перевозчика.**
   Транзакция запроса откатилась бы вместе с ним при любом сбое — именно
   тогда, когда запись о намерении нужна больше всего. Отсюда следует
   главное: если строки нет, то и обращения к перевозчику не было.
2. **Номер выдаётся до обращения** и уходит перевозчику как номер клиента.
   По нему заказ потом и находится.
3. **Повтор начинается со сверки**: адаптер спрашивает перевозчика о заказе
   с нашим номером. Нашёлся — принимаем его в базу, а не создаём второй.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.carriers import registry
from aerogram.carriers.base import CarrierAccount as AdapterAccount
from aerogram.carriers.base import CarrierAdapter, Place, ShipmentRequest, ShipmentResult
from aerogram.config import Settings
from aerogram.core.models import CarrierAccount
from aerogram.core.repository import CarrierAccountRepository
from aerogram.core.service import decrypt_credentials
from aerogram.db import session_scope
from aerogram.directories.dadata import DadataClient
from aerogram.directories.repository import CarrierRepository
from aerogram.directories.service import CarrierPartyResolver, CityMappingService, CityService
from aerogram.rating.repository import RateRepository
from aerogram.rating.schemas import RateRequestIn
from aerogram.routing.repository import RoutingRepository
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import EventSource, ShipmentStatus
from aerogram.shared.errors import CarrierNotConfigured, Conflict, NotFound, ValidationFailed
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money, mm_to_cm
from aerogram.shared.schemas import MoneySchema
from aerogram.shipments.models import Shipment
from aerogram.shipments.repository import ShipmentRepository
from aerogram.shipments.schemas import (
    CreateShipmentRequest,
    ShipmentOut,
    ShipmentPage,
    contract_status,
)
from aerogram.tracking.service import TrackingService, next_poll_after

__all__ = ["NUMBER_PREFIX", "ShipmentService", "shipment_number"]

log = get_logger(__name__)

#: Префикс внутреннего номера. Виден оператору и уходит перевозчику,
#: поэтому короткий и узнаваемый.
NUMBER_PREFIX = "AG"

#: Сколько шестнадцатеричных знаков берётся от отпечатка. Двенадцать — это
#: 48 бит: столкновение внутри одного тенанта неправдоподобно, а номер
#: остаётся читаемым вслух по телефону.
_NUMBER_DIGITS = 12

#: Состояния, из которых отменять уже нечего.
_FINAL_STATES = frozenset(
    {ShipmentStatus.DELIVERED, ShipmentStatus.RETURNED, ShipmentStatus.CANCELLED}
)


def shipment_number(tenant_id: UUID, idempotency_key: str) -> str:
    """Номер отправления, выведенный из ключа идемпотентности.

    Номер выводится из ключа, а не берётся случайным, чтобы повтор давал тот
    же номер при любом состоянии базы: разбирать инцидент по журналу клиента
    и по журналу перевозчика проще, когда номер один и тот же.

    Тенант входит в отпечаток, чтобы одинаковые ключи разных клиентов
    не давали одинаковых номеров: номер уникален в пределах тенанта,
    но уходит к перевозчику, где тенанты не различаются.
    """
    digest = hashlib.sha256(f"{tenant_id}|{idempotency_key}".encode()).hexdigest()
    return f"{NUMBER_PREFIX}-{digest[:_NUMBER_DIGITS].upper()}"


class ShipmentService:
    """Отправления тенанта."""

    def __init__(
        self, session: AsyncSession, settings: Settings, dadata: DadataClient | None = None
    ) -> None:
        self._session = session
        self._settings = settings
        self._shipments = ShipmentRepository(session)
        self._rates = RateRepository(session)
        self._routing = RoutingRepository(session)
        self._accounts = CarrierAccountRepository(session)
        self._carriers = CarrierRepository(session)
        self._parties = CarrierPartyResolver(
            CityService(session, dadata), CityMappingService(session)
        )

    # --- Чтение -----------------------------------------------------------

    async def get(self, shipment_id: UUID) -> ShipmentOut:
        shipment = await self._shipments.get(shipment_id)
        if shipment is None:
            # Чужое отправление RLS не отдаёт вовсе, и это тот же 404:
            # наличие объекта у соседнего тенанта — не то, что стоит
            # подтверждать (раздел 7.2 ТЗ).
            raise NotFound("Отправление не найдено")
        return await self._to_out(shipment)

    async def page(
        self,
        *,
        status: str | None,
        carrier_id: UUID | None,
        q: str | None,
        page: int,
        page_size: int,
    ) -> ShipmentPage:
        rows, total = await self._shipments.page(
            status=status, carrier_id=carrier_id, q=q, page=page, page_size=page_size
        )
        names = {c.id: c.name for c in await self._carriers.list_active()}
        promises = await self._rates.promises_by_offer(
            [row.rate_offer_id for row in rows if row.rate_offer_id is not None]
        )
        return ShipmentPage(
            items=[
                _to_out(
                    row,
                    names.get(row.carrier_id),
                    promises.get(row.rate_offer_id, (None, None))
                    if row.rate_offer_id
                    else (None, None),
                )
                for row in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- Создание ---------------------------------------------------------

    async def create(
        self,
        payload: CreateShipmentRequest,
        *,
        tenant_id: UUID,
        user_id: UUID | None,
        idempotency_key: str,
    ) -> ShipmentOut:
        """Создать отправление по принятому решению.

        Повтор с тем же ключом не создаёт второго заказа: либо возвращает
        готовое отправление, либо доводит до конца незавершённую попытку.
        """
        number = payload.external_id or shipment_number(tenant_id, idempotency_key)

        existing = await self._shipments.by_idempotency_key(idempotency_key)
        if existing is not None:
            _ensure_same_request(existing, payload, number)
            if existing.external_id is not None:
                return await self._to_out(existing)
            # Первая попытка не дошла до подтверждения. Повторять создание
            # вслепую нельзя — сначала спрашиваем перевозчика.
            return await self._finish(existing, tenant_id, reconcile=True)

        decision, account = await self._prepare(payload, number)

        draft = Shipment(
            id=uuid7(),
            tenant_id=tenant_id,
            number=number,
            carrier_id=account.carrier_id,
            carrier_account_id=account.id,
            decision_id=decision.id,
            rate_offer_id=decision.selected_offer_id,
            status=ShipmentStatus.DRAFT,
            idempotency_key=idempotency_key,
            created_by_user_id=user_id,
        )
        await self._fill_from_quote(draft, decision.selected_offer_id)

        # Своя транзакция и коммит: транзакция запроса откатилась бы вместе
        # с черновиком при любом сбое — ровно в тот момент, когда запись
        # о нашем намерении нужна больше всего.
        async with session_scope(tenant_id) as scope:
            ShipmentRepository(scope).add(draft)
        log.info("shipment.drafted", number=draft.number, carrier_id=str(draft.carrier_id))

        # Сверка здесь не нужна и была бы лишним обращением к перевозчику:
        # черновик только что записан, а значит по этому номеру мы к ТК
        # ещё не обращались ни разу.
        return await self._finish(draft, tenant_id, reconcile=False)

    async def _prepare(
        self, payload: CreateShipmentRequest, number: str
    ) -> tuple[Any, CarrierAccount]:
        """Проверить решение и учётную запись до того, как что-то писать."""
        decision = await self._routing.get_decision(payload.decision_id)
        if decision is None:
            raise NotFound("Решение не найдено")

        already = await self._shipments.by_decision(decision.id)
        if already is not None:
            # Одно решение — одно отправление. Второе означало бы два заказа
            # у перевозчика на один груз.
            raise Conflict(
                f"По этому решению уже создано отправление {already.number}",
                field="decision_id",
            )
        if await self._shipments.by_number(number) is not None:
            raise Conflict(f"Отправление с номером {number} уже существует", field="external_id")

        offer = await self._rates.get_offer(decision.selected_offer_id)
        if offer is None:
            raise NotFound("Предложение не найдено")
        if offer.carrier_account_id is None:
            raise Conflict("У предложения нет учётной записи перевозчика", field="decision_id")

        account = await self._accounts.get_by_id(offer.carrier_account_id)
        if account is None or not account.is_active:
            raise Conflict("Учётная запись перевозчика недоступна", field="decision_id")
        return decision, account

    async def _fill_from_quote(self, shipment: Shipment, offer_id: UUID) -> None:
        """Перенести в отправление то, что было обещано на расчёте.

        Обещанная цена сохраняется отдельно от фактической: расхождение
        между ними — предмет сверки счетов, и затирать обещание фактом
        значит потерять эту сверку.
        """
        offer = await self._rates.get_offer(offer_id)
        if offer is None:  # pragma: no cover - проверено в _prepare
            raise NotFound("Предложение не найдено")
        shipment.service_code = offer.service_code
        shipment.tariff_code = offer.tariff_code
        shipment.currency = offer.currency
        shipment.price_quoted_amount_minor = offer.total_amount_minor
        shipment.promised_delivery_date = offer.promised_delivery_date
        shipment.transit_days_planned = offer.transit_days_max

        quote = await self._rates.get_quote(offer.quote_id)
        if quote is not None:
            request = RateRequestIn.model_validate(quote.input_snapshot)
            shipment.declared_value_amount_minor = request.cargo_value.amount_minor
            shipment.sender_snapshot = request.origin.model_dump(mode="json")
            shipment.recipient_snapshot = request.destination.model_dump(mode="json")

    async def _finish(self, shipment: Shipment, tenant_id: UUID, *, reconcile: bool) -> ShipmentOut:
        """Довести черновик до подтверждённого заказа.

        ``reconcile`` включается на повторе: если предыдущая попытка успела
        отправить запрос, заказ у перевозчика уже есть, и второй был бы
        дублем — с оплатой и вторым грузом. На свежем черновике сверять
        нечего, и лишний вызов только удвоил бы задержку создания.
        """
        adapter, account = await self._adapter_for(shipment)
        result = await adapter.find_by_number(shipment.number, account) if reconcile else None
        reconciled = result is not None
        if result is None:
            result = await adapter.create(await self._build_request(shipment), account)

        async with session_scope(tenant_id) as scope:
            stored = await ShipmentRepository(scope).get(shipment.id)
            if stored is None:  # pragma: no cover - строку только что записали
                raise NotFound("Отправление не найдено")
            _apply(stored, result)
            fresh = _to_out(stored, None)

        log.info(
            "shipment.created",
            number=shipment.number,
            reconciled=reconciled,
            pending=result.is_pending,
        )
        return fresh.model_copy(
            update={
                "carrier_name": await self._carrier_name(shipment),
                "eta": (promise := await self._promise(shipment))[0],
                "deadline": promise[1],
            }
        )

    # --- Отмена -----------------------------------------------------------

    async def cancel(self, shipment_id: UUID, *, tenant_id: UUID) -> ShipmentOut:
        """Отменить отправление, пока перевозчик это принимает (FR-2.6)."""
        shipment = await self._shipments.get(shipment_id)
        if shipment is None:
            raise NotFound("Отправление не найдено")
        if shipment.status == ShipmentStatus.CANCELLED:
            # Повторная отмена — не ошибка: результат уже достигнут.
            return await self._to_out(shipment)
        if ShipmentStatus(shipment.status) in _FINAL_STATES:
            raise Conflict("Отправление уже завершено, отменять нечего", field="status")

        adapter, account = await self._adapter_for(shipment)
        if not adapter.capabilities.supports_cancel:
            raise Conflict("Перевозчик не принимает отмену", field="carrier_id")

        external_id = shipment.external_id
        if external_id is None:
            # Черновик: заказа у перевозчика может не быть вовсе, а может
            # и быть — от попытки, чей ответ не дошёл. Бросить существующий
            # заказ значит оставить «призрака».
            found = await adapter.find_by_number(shipment.number, account)
            external_id = found.external_id if found is not None else None

        if external_id is not None:
            outcome = await adapter.cancel(external_id, account)
            if not outcome.accepted:
                raise Conflict(
                    outcome.message or "Перевозчик отказал в отмене", field="shipment_id"
                )

        async with session_scope(tenant_id) as scope:
            stored = await ShipmentRepository(scope).get(shipment_id)
            if stored is None:  # pragma: no cover
                raise NotFound("Отправление не найдено")
            if external_id is not None:
                stored.external_id = external_id
            stored.status = ShipmentStatus.CANCELLED
            stored.cancelled_at = utcnow()
            fresh = _to_out(stored, None)

        log.info("shipment.cancelled", number=shipment.number, at_carrier=external_id is not None)
        return fresh.model_copy(
            update={
                "carrier_name": await self._carrier_name(shipment),
                "eta": (promise := await self._promise(shipment))[0],
                "deadline": promise[1],
            }
        )

    # --- Трекинг ----------------------------------------------------------

    async def poll(self, shipment: Shipment) -> int:
        """Спросить у перевозчика события отправления и принять их в ленту.

        Живёт здесь, а не в ``tracking``: адаптер и расшифрованные учётные
        данные уже есть тут, а трекинг сознательно не ходит к перевозчикам —
        он нормализует и хранит то, что ему принесли.
        """
        adapter, account = await self._adapter_for(shipment)
        if shipment.external_id is None:
            # Заказа у перевозчика нет — спрашивать не о чем. Это черновик,
            # и им занимается сверка «призраков», а не опрос статусов.
            return 0
        events = await adapter.track(shipment.external_id, account)
        return await TrackingService(self._session).ingest(
            shipment,
            events,
            carrier_code=account.carrier_code,
            source=EventSource.API_POLL,
        )

    # --- Сверка «призраков» -----------------------------------------------

    async def reconcile_unconfirmed(self, *, tenant_id: UUID, limit: int = 100) -> int:
        """Догнать черновики, чей ответ не дошёл. Возвращает число найденных.

        Вызывается фоновой задачей: клиент мог не повторить запрос вовсе,
        а заказ у перевозчика при этом существует.
        """
        found = 0
        for shipment in await self._shipments.unconfirmed(limit):
            try:
                adapter, account = await self._adapter_for(shipment)
                result = await adapter.find_by_number(shipment.number, account)
            except Exception as exc:
                # Сбой по одному перевозчику не должен останавливать сверку
                # по остальным: непроверенный черновик и есть будущий призрак.
                log.warning(
                    "shipment.reconcile_failed",
                    number=shipment.number,
                    error_type=type(exc).__name__,
                )
                continue
            if result is None:
                continue
            async with session_scope(tenant_id) as scope:
                stored = await ShipmentRepository(scope).get(shipment.id)
                if stored is not None:
                    _apply(stored, result)
            found += 1
            log.info("shipment.reconciled", number=shipment.number)
        return found

    # --- Вспомогательное --------------------------------------------------

    async def _adapter_for(self, shipment: Shipment) -> tuple[CarrierAdapter, AdapterAccount]:
        """Адаптер и расшифрованная учётная запись для отправления."""
        if shipment.carrier_account_id is None:
            raise Conflict("У отправления нет учётной записи перевозчика", field="carrier_id")
        account = await self._accounts.get_by_id(shipment.carrier_account_id)
        if account is None or not account.is_active:
            raise Conflict("Учётная запись перевозчика недоступна", field="carrier_id")

        carrier = next(
            (c for c in await self._carriers.list_active() if c.id == account.carrier_id), None
        )
        if carrier is None:
            raise Conflict("Перевозчик отключён", field="carrier_id")
        try:
            adapter = registry.get_adapter(carrier.code)
        except LookupError:
            raise CarrierNotConfigured(
                "Перевозчик не подключён к платформе", carrier_code=carrier.code
            ) from None

        return adapter, AdapterAccount(
            account_id=str(account.id),
            carrier_code=carrier.code,
            mode=account.mode,  # type: ignore[arg-type]
            credentials=decrypt_credentials(account, self._settings),
            is_sandbox=account.is_sandbox,
            settings=dict(account.settings or {}),
        )

    async def _build_request(self, shipment: Shipment) -> ShipmentRequest:
        """Запрос к перевозчику из снимка расчёта.

        Габариты и адреса берутся из снимка, а не из текущего состояния
        адресной книги: заказ обязан соответствовать тому, что посчитали.
        """
        offer = (
            await self._rates.get_offer(shipment.rate_offer_id)
            if shipment.rate_offer_id is not None
            else None
        )
        quote = await self._rates.get_quote(offer.quote_id) if offer is not None else None
        if quote is None:
            raise ValidationFailed("Снимок расчёта не найден", field="decision_id")

        request = RateRequestIn.model_validate(quote.input_snapshot)
        return ShipmentRequest(
            number=shipment.number,
            service_code=shipment.service_code or "",
            tariff_code=shipment.tariff_code or "",
            sender=await self._parties.party(request.origin, shipment.carrier_id),
            recipient=await self._parties.party(request.destination, shipment.carrier_id),
            places=tuple(
                Place(
                    weight_kg=package.weight_kg,
                    length_cm=mm_to_cm(package.length_mm),
                    width_cm=mm_to_cm(package.width_mm),
                    height_cm=mm_to_cm(package.height_mm),
                )
                for package in request.packages
            ),
            declared_value=request.cargo_value.to_money(),
            cargo_type=request.cargo_type,
            pickup=request.pickup,
            delivery_to_door=request.delivery_to_door,
            insurance=request.insurance,
        )

    async def _carrier_name(self, shipment: Shipment) -> str | None:
        carriers = await self._carriers.list_active()
        return next((c.name for c in carriers if c.id == shipment.carrier_id), None)

    async def _promise(self, shipment: Shipment) -> tuple[datetime | None, datetime | None]:
        """Ожидаемая дата и крайний срок из снимка расчёта."""
        if shipment.rate_offer_id is None:
            return (None, None)
        promises = await self._rates.promises_by_offer([shipment.rate_offer_id])
        return promises.get(shipment.rate_offer_id, (None, None))

    async def _to_out(self, shipment: Shipment) -> ShipmentOut:
        return _to_out(shipment, await self._carrier_name(shipment), await self._promise(shipment))


def _ensure_same_request(existing: Shipment, payload: CreateShipmentRequest, number: str) -> None:
    """Тот же ключ с другим содержимым — 409.

    Сверяются сами поля, а не их отпечаток: тело запроса состоит из решения
    и номера, и хранить рядом ещё и хеш значило бы завести колонку ради того,
    что и так лежит в строке.
    """
    if existing.decision_id != payload.decision_id or existing.number != number:
        raise Conflict(
            "Этот ключ идемпотентности уже использован с другим содержимым запроса",
            field="Idempotency-Key",
        )


def _apply(shipment: Shipment, result: ShipmentResult) -> None:
    """Ответ перевозчика → строка отправления."""
    shipment.external_id = result.external_id
    shipment.tracking_number = result.tracking_number
    if result.promised_delivery_date is not None:
        shipment.promised_delivery_date = result.promised_delivery_date
    if result.price_actual is not None:
        shipment.price_actual_amount_minor = result.price_actual.amount_minor
    # ``is_pending`` — трек-номер придёт позже, но заказ уже принят: это
    # ACCEPTED, а не CREATED, и разница видна в ленте статусов.
    shipment.status = ShipmentStatus.ACCEPTED if result.is_pending else ShipmentStatus.CREATED
    # Ставим отправление в очередь опроса прямо здесь. Расписание пересчитывается
    # при каждом событии, но ПЕРВОГО события неоткуда взяться: пока срок опроса
    # не назначен, задача это отправление не видит, и трекинг не начинается
    # никогда. Обнаружено на стенде — тестами не ловилось, потому что
    # проверялись создание и приём событий по отдельности.
    shipment.next_poll_at, _ = next_poll_after(ShipmentStatus(shipment.status), None, utcnow())


def _to_out(
    shipment: Shipment,
    carrier_name: str | None,
    promise: tuple[datetime | None, datetime | None] = (None, None),
) -> ShipmentOut:
    quoted = Money(shipment.price_quoted_amount_minor or 0, shipment.currency)
    actual = (
        Money(shipment.price_actual_amount_minor, shipment.currency)
        if shipment.price_actual_amount_minor is not None
        else None
    )
    return ShipmentOut(
        id=shipment.id,
        number=shipment.number,
        external_id=shipment.external_id,
        decision_id=shipment.decision_id,
        carrier_id=shipment.carrier_id,
        carrier_name=carrier_name,
        tracking_number=shipment.tracking_number,
        status=contract_status(shipment.status),
        eta=promise[0],
        deadline=promise[1],
        quoted_total_cost=MoneySchema.of(quoted),
        actual_total_cost=MoneySchema.of(actual) if actual is not None else None,
        created_at=shipment.created_at,
    )
