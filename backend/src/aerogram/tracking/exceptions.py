"""Разбор исключений: что из едущего требует внимания оператора (раздел 10 ТЗ).

Список собирается из уже записанных фактов и ничего не пересчитывает: адаптеры
здесь не вызываются, в базу не пишется ни строки. Причина в том, что экран
разбора открывают чаще всего именно тогда, когда с перевозчиком что-то не так,
и поход к нему за данными сделал бы неработающим ровно тот инструмент, которым
неработающее и разбирают.

Три причины, и все три выводятся из данных, а не из настройки:

* ``deadline_passed`` — срок из снимка расчёта прошёл, а доставки нет.
  Это единственное место, где такое видно: ``shipments.is_late``
  проставляется в момент доставки, то есть постфактум, и до неё сорванный
  срок не помечен ничем.
* ``stalled`` — перевозчик молчит дольше порога адаптивного опроса.
  Порог уже есть в ``next_poll_after`` и здесь не дублируется.
* ``problem_status`` — состояние само по себе означает разбор: неудачная
  попытка вручения, возврат, исключение перевозчика.

Порог тишины принадлежит опросу, а «риск срыва» как отдельная политика
(за сколько часов до срока считать отправление рискованным) не определён
и здесь не выдумывается — см. docs/status.md.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.directories.repository import CarrierRepository
from aerogram.rating.repository import RateRepository
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import ShipmentStatus
from aerogram.shipments.models import Shipment
from aerogram.shipments.repository import ShipmentRepository
from aerogram.shipments.schemas import contract_status
from aerogram.tracking.schemas import ShipmentExceptionOut, ShipmentExceptionsPage
from aerogram.tracking.service import PROBLEM_STATES, STALLED_INCIDENT

__all__ = [
    "REASON_DEADLINE_PASSED",
    "REASON_PROBLEM_STATUS",
    "REASON_STALLED",
    "SCAN_LIMIT",
    "ExceptionService",
]

REASON_DEADLINE_PASSED = "deadline_passed"
REASON_STALLED = "stalled"
REASON_PROBLEM_STATUS = "problem_status"

#: Порядок разбора: сорванный срок дороже всего, зависшее — дешевле всего.
#: Оператор читает список сверху, и порядок здесь — это порядок работы.
_SEVERITY = {
    REASON_DEADLINE_PASSED: 0,
    REASON_PROBLEM_STATUS: 1,
    REASON_STALLED: 2,
}

#: Сколько едущих отправлений просматривается за раз. Ограничение существует
#: не ради экономии, а ради честного ответа: без него страница молча
#: замедлялась бы вместе с ростом клиента. Переполнение видно в ``truncated``.
SCAN_LIMIT = 500


class ExceptionService:
    """Отправления, требующие вмешательства. Только чтение."""

    def __init__(self, session: AsyncSession) -> None:
        self._shipments = ShipmentRepository(session)
        self._rates = RateRepository(session)
        self._carriers = CarrierRepository(session)

    async def list_open(self, *, limit: int = SCAN_LIMIT) -> ShipmentExceptionsPage:
        """Собрать список исключений среди едущих отправлений."""
        now = utcnow()
        # Запрашивается на одну строку больше предела: так видно, что предел
        # достигнут, и не приходится делать отдельный COUNT.
        scanned = await self._shipments.active(limit + 1)
        truncated = len(scanned) > limit
        rows = scanned[:limit]

        promises = await self._rates.promises_by_offer(
            [row.rate_offer_id for row in rows if row.rate_offer_id is not None]
        )
        names = {c.id: c.name for c in await self._carriers.list_active()}

        items: list[ShipmentExceptionOut] = []
        for row in rows:
            deadline = (
                promises.get(row.rate_offer_id, (None, None))[1]
                if row.rate_offer_id is not None
                else None
            )
            reasons = _reasons(row, deadline, now)
            if not reasons:
                continue
            items.append(
                ShipmentExceptionOut(
                    id=row.id,
                    number=row.number,
                    carrier_name=names.get(row.carrier_id),
                    tracking_number=row.tracking_number,
                    status=contract_status(row.status),
                    deadline=deadline,
                    last_event_at=row.last_event_at,
                    reasons=reasons,
                )
            )

        items.sort(key=_order)
        return ShipmentExceptionsPage(
            items=items,
            total=len(items),
            scanned=len(rows),
            truncated=truncated,
            by_reason=_counters(items),
        )


def _reasons(shipment: Shipment, deadline: datetime | None, now: datetime) -> list[str]:
    """Причины разбора для одного отправления, от дорогой к дешёвой."""
    reasons: list[str] = []
    if deadline is not None and deadline < now:
        # Доставленное сюда не попадает: список строится по едущим.
        reasons.append(REASON_DEADLINE_PASSED)
    if ShipmentStatus(shipment.status) in PROBLEM_STATES:
        reasons.append(REASON_PROBLEM_STATUS)
    if shipment.has_incident and shipment.incident_type == STALLED_INCIDENT:
        reasons.append(REASON_STALLED)
    return reasons


def _order(item: ShipmentExceptionOut) -> tuple[int, float]:
    """Сначала по тяжести причины, внутри — кто дольше молчит.

    Отправление без единого события считается молчащим дольше всех: о нём
    неизвестно вообще ничего, и это худший случай, а не лучший.
    """
    severity = min(_SEVERITY[reason] for reason in item.reasons)
    silence = item.last_event_at.timestamp() if item.last_event_at is not None else float("-inf")
    return severity, silence


def _counters(items: list[ShipmentExceptionOut]) -> dict[str, int]:
    """Сколько отправлений по каждой причине.

    Сумма счётчиков больше числа строк, когда у отправления причин несколько,
    — и это верно: срыв срока и молчание перевозчика разбираются по-разному.
    """
    counters = dict.fromkeys(_SEVERITY, 0)
    for item in items:
        for reason in item.reasons:
            counters[reason] += 1
    return counters
