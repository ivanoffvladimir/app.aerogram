"""Единая лента трекинга: приём событий, нормализация, проекция на отправление.

Модуль сам к перевозчикам не ходит. Он принимает уже полученные события —
вебхуком или опросом — и отвечает за то, чтобы лента была одинаковой
независимо от ТК (FR-3.4), а сырой статус сохранялся всегда (FR-3.3).

Два свойства, которые легко потерять:

* **Порядок по времени события, а не по времени получения.** Перевозчики
  регулярно отдают события с задержкой и не по порядку. Лента, собранная
  по получению, покажет доставку раньше отправки, а статус отправления
  «откатится» назад при догоняющем старом событии.
* **Дубли не создают второй строки.** Одно и то же событие приходит и
  вебхуком, и опросом; отпечаток события — единственное, что их различает.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.carriers.base import RawEvent
from aerogram.carriers.status_map import load_status_map, normalize_status
from aerogram.config import Settings
from aerogram.rating.repository import RateRepository
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import EventSource, ShipmentStatus
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shipments.models import Shipment
from aerogram.shipments.repository import ShipmentRepository
from aerogram.tracking.models import DeliveryOutcome, ShipmentEvent
from aerogram.tracking.repository import TrackingRepository
from aerogram.tracking.schemas import TrackingEventOut
from aerogram.tracking.webhooks import WebhookService

__all__ = [
    "POLL_INTERVALS",
    "PROBLEM_STATES",
    "STALE_AFTER",
    "STALLED_INCIDENT",
    "TrackingService",
    "next_poll_after",
]

log = get_logger(__name__)

#: Частота опроса по состоянию отправления (FR-3.2). Ключ ``None`` означает
#: «опрос прекращается»: у финального статуса спрашивать больше нечего.
POLL_INTERVALS: dict[ShipmentStatus, timedelta | None] = {
    # Создано, груз не забран.
    ShipmentStatus.DRAFT: timedelta(hours=1),
    ShipmentStatus.CREATED: timedelta(hours=1),
    ShipmentStatus.ACCEPTED: timedelta(hours=1),
    # В пути.
    ShipmentStatus.PICKED_UP: timedelta(hours=3),
    ShipmentStatus.AT_ORIGIN_HUB: timedelta(hours=3),
    ShipmentStatus.IN_TRANSIT: timedelta(hours=3),
    ShipmentStatus.RETURN_IN_PROGRESS: timedelta(hours=3),
    ShipmentStatus.EXCEPTION: timedelta(hours=3),
    # В городе назначения и на доставке: здесь всё решают минуты, и здесь же
    # оператор чаще всего вмешивается вручную.
    ShipmentStatus.AT_DESTINATION_HUB: timedelta(minutes=30),
    ShipmentStatus.OUT_FOR_DELIVERY: timedelta(minutes=30),
    ShipmentStatus.READY_FOR_PICKUP: timedelta(minutes=30),
    ShipmentStatus.DELIVERY_ATTEMPT_FAILED: timedelta(minutes=30),
    # Финальные.
    ShipmentStatus.DELIVERED: None,
    ShipmentStatus.RETURNED: None,
    ShipmentStatus.CANCELLED: None,
}

#: Состояния, о которых получателя уведомляют как о проблеме (FR-3.6).
#: Состояния, которые сами по себе означают разбор. Набор общий с разбором
#: исключений (``tracking.exceptions``): разойдись он — экран оператора
#: и уведомление тенанту говорили бы о разном.
PROBLEM_STATES = frozenset(
    {
        ShipmentStatus.EXCEPTION,
        ShipmentStatus.DELIVERY_ATTEMPT_FAILED,
        ShipmentStatus.RETURN_IN_PROGRESS,
        ShipmentStatus.RETURNED,
    }
)

#: После скольких суток тишины отправление считается зависшим (FR-3.2).
STALE_AFTER = timedelta(days=5)

#: Как часто опрашивать зависшее: раз в сутки. Чаще бессмысленно — событий нет,
#: и учащённый опрос лишь расходует лимит перевозчика.
STALE_INTERVAL = timedelta(days=1)

#: Тип инцидента для зависшего отправления. Отдельным типом, а не общим
#: признаком: «зависло» требует звонка перевозчику, а не работы с грузом.
STALLED_INCIDENT = "stalled"


def next_poll_after(
    status: ShipmentStatus, last_event_at: datetime | None, now: datetime
) -> tuple[datetime | None, bool]:
    """Когда опрашивать следующий раз и зависло ли отправление (FR-3.2).

    ``None`` в первом элементе означает «опрос прекращается»: статус
    финальный, и спрашивать больше нечего.

    Тишина дольше пяти суток переводит на суточный опрос и поднимает флаг:
    отсутствие событий само по себе новость, и молчать о ней хуже, чем
    показать оператору, что с отправлением что-то не так.
    """
    interval = POLL_INTERVALS.get(status, timedelta(hours=3))
    if interval is None:
        return None, False

    stale = last_event_at is not None and now - last_event_at > STALE_AFTER
    return now + (STALE_INTERVAL if stale else interval), stale


def _mapper_for(carrier_code: str) -> Callable[[str], tuple[ShipmentStatus, bool]]:
    """Нормализатор статусов перевозчика.

    Карты может не быть — это наша ошибка настройки, а не перевозчика,
    и ронять из-за неё приём событий нельзя: заказ едет, статусы идут,
    и потерять их значит потерять историю доставки. Событие сохраняется
    с сырым статусом и признаком «не сопоставлено», то есть попадает
    в очередь ручного разбора — там его и увидят.
    """
    try:
        load_status_map(carrier_code)
    except FileNotFoundError:
        log.error("tracking.no_status_map", carrier=carrier_code)
        return lambda _: (ShipmentStatus.IN_TRANSIT, True)
    return lambda status_raw: normalize_status(carrier_code, status_raw)


class TrackingService:
    """Лента событий отправления."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._tracking = TrackingRepository(session)
        self._shipments = ShipmentRepository(session)
        # Без настроек вебхуки не ставятся: шифрование секрета без ключей
        # невозможно. Так модуль остаётся вызываемым там, где уведомления
        # не нужны — например, в проверке ленты.
        self._webhooks = WebhookService(session, settings) if settings is not None else None

    async def timeline(self, shipment_id: UUID) -> list[TrackingEventOut]:
        """Лента в едином виде, независимо от перевозчика (FR-3.4)."""
        events = await self._tracking.timeline(shipment_id)
        return [
            TrackingEventOut(
                occurred_at=event.occurred_at,
                normalized_status=str(event.status_normalized),
                carrier_status=event.status_raw,
                location=event.city,
                description=event.comment,
            )
            for event in events
        ]

    async def ingest(
        self,
        shipment: Shipment,
        raw_events: Sequence[RawEvent],
        *,
        carrier_code: str,
        source: EventSource,
    ) -> int:
        """Принять события перевозчика. Возвращает число новых.

        Повторное событие не создаёт второй строки и не считается новым:
        одно и то же приходит и вебхуком, и опросом.
        """
        known = await self._tracking.known_keys(shipment.id)
        fresh: list[ShipmentEvent] = []
        mapper = _mapper_for(carrier_code)

        for raw in raw_events:
            key = raw.dedup_key()
            if key in known:
                continue
            # Пачка может содержать дубли внутри себя: без этого уникальный
            # индекс откатил бы всю пачку из-за одного повторного события.
            known.add(key)
            status, unmapped = mapper(raw.status_raw)
            if unmapped:
                # Не роняем обработку: статус пишется как есть, событие
                # получает IN_TRANSIT и попадает в очередь ручного разбора.
                log.warning(
                    "tracking.unmapped_status", carrier=carrier_code, status_raw=raw.status_raw
                )
            fresh.append(
                ShipmentEvent(
                    id=uuid7(),
                    tenant_id=shipment.tenant_id,
                    shipment_id=shipment.id,
                    occurred_at=raw.occurred_at,
                    status_normalized=status,
                    status_raw=raw.status_raw,
                    is_unmapped=unmapped,
                    city=raw.city,
                    comment=raw.comment,
                    source=source,
                    dedup_key=key,
                    raw=dict(raw.raw) or None,
                )
            )

        if fresh:
            self._tracking.add_events(fresh)
            await self._session.flush()

        await self._project(shipment)
        log.info(
            "tracking.ingested",
            number=shipment.number,
            source=source.value,
            new=len(fresh),
            seen=len(raw_events),
        )
        return len(fresh)

    async def due_for_poll(self, limit: int = 200) -> list[Shipment]:
        """Отправления, которым пора опросить статус."""
        return await self._shipments.due_for_poll(limit)

    async def _project(self, shipment: Shipment) -> None:
        """Пересчитать состояние отправления по всей ленте.

        Считается по ленте целиком, а не по последней пачке: догоняющее старое
        событие не должно откатывать статус назад, а пропущенный забор груза
        обязан проставиться, даже если пришёл позже доставки.
        """
        events = await self._tracking.timeline(shipment.id)
        now = utcnow()
        if not events:
            shipment.next_poll_at, _ = next_poll_after(
                ShipmentStatus(shipment.status), shipment.last_event_at, now
            )
            return

        latest = events[-1]
        # Прошлое состояние запоминается ДО присваивания: уведомление
        # «статус изменился» на неизменившемся статусе — шум, из-за которого
        # получатель перестаёт читать уведомления вообще.
        previous = ShipmentStatus(shipment.status)
        was_late = bool(shipment.is_late)
        shipment.status = latest.status_normalized
        shipment.carrier_status_raw = latest.status_raw
        shipment.last_event_at = latest.occurred_at

        pickup = next((e for e in events if e.status_normalized == ShipmentStatus.PICKED_UP), None)
        if pickup is not None and shipment.picked_up_at is None:
            shipment.picked_up_at = pickup.occurred_at

        delivered = next(
            (e for e in events if e.status_normalized == ShipmentStatus.DELIVERED), None
        )
        if delivered is not None:
            await self._settle(shipment, delivered.occurred_at)

        shipment.next_poll_at, stale = next_poll_after(
            ShipmentStatus(shipment.status), shipment.last_event_at, now
        )
        if stale and not shipment.has_incident:
            shipment.has_incident = True
            shipment.incident_type = STALLED_INCIDENT
            log.warning("tracking.stalled", number=shipment.number)

        await self._notify(shipment, previous, was_late)

    async def _notify(self, shipment: Shipment, previous: ShipmentStatus, was_late: bool) -> None:
        """Поставить в очередь исходящие уведомления (FR-3.6).

        Ставится в той же транзакции, что и изменение отправления: иначе сбой
        отправки откатил бы приём события, и статус, который перевозчик уже
        сообщил, был бы потерян ради уведомления.
        """
        if self._webhooks is None:
            return

        current = ShipmentStatus(shipment.status)
        if current == previous:
            return

        await self._webhooks.enqueue(shipment, "shipment.status_changed")
        if current is ShipmentStatus.DELIVERED:
            await self._webhooks.enqueue(shipment, "shipment.delivered")
        if current in PROBLEM_STATES:
            await self._webhooks.enqueue(shipment, "shipment.exception")
        # Опоздание — отдельное событие: доставленное с опозданием всё равно
        # доставлено, и по одному лишь статусу этого не увидеть.
        if shipment.is_late and not was_late:
            await self._webhooks.enqueue(shipment, "shipment.delayed")

    async def _settle(self, shipment: Shipment, delivered_at: datetime) -> None:
        """Зафиксировать факт доставки — вход обучающего датасета.

        ``DeliveryOutcome`` создаётся один раз: две строки об одной доставке
        означали бы два противоречащих факта, и именно поэтому первичный ключ
        таблицы — сам ``shipment_id``.
        """
        shipment.actual_delivery_date = delivered_at.date()
        if shipment.picked_up_at is not None:
            shipment.transit_days_actual = (delivered_at - shipment.picked_up_at).days

        deadline = await self._deadline(shipment)
        met: bool | None = None
        delay_seconds: int | None = None
        if deadline is not None:
            # Пустой запас — это «уложились»: срок «до 23:59» включает 23:59.
            overshoot = int((delivered_at - deadline).total_seconds())
            met = overshoot <= 0
            delay_seconds = max(overshoot, 0)
            shipment.is_late = not met
            shipment.delay_days = delay_seconds // 86_400

        outcome = await self._tracking.outcome(shipment.id)
        if outcome is None:
            outcome = self._tracking.add_outcome(
                DeliveryOutcome(shipment_id=shipment.id, tenant_id=shipment.tenant_id)
            )
        outcome.delivered_at = delivered_at
        outcome.deadline_met = met
        outcome.delay_seconds = delay_seconds

    async def _deadline(self, shipment: Shipment) -> datetime | None:
        """Крайний срок из снимка расчёта.

        Он живёт в снимке, а не в отправлении: срок ставил клиент на расчёте,
        и переписывать его задним числом означало бы менять условие, по которому
        считается соблюдение SLA.
        """
        if shipment.rate_offer_id is None:
            return None
        promises = await RateRepository(self._session).promises_by_offer([shipment.rate_offer_id])
        deadline = promises.get(shipment.rate_offer_id, (None, None))[1]
        # Время в базе хранится с зоной; страховка на случай наивного значения
        # из старого снимка — сравнение наивного с зонированным упало бы.
        if deadline is not None and deadline.tzinfo is None:
            return deadline.replace(tzinfo=UTC)
        return deadline
