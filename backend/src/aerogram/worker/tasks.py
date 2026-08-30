"""Фоновые задачи: опрос статусов, сверка «призраков», пересчёт скора.

**Как это работает с изоляцией тенантов.** У фоновой задачи нет запроса,
а значит и тенанта, но роль приложения работает под `FORCE ROW LEVEL SECURITY`
и без установленного `app.tenant_id` не видит ничего. Ни новой роли, ни нового
окна для этого не нужно: таблица `tenants` платформенная и RLS на неё
не распространяется, поэтому задача перечисляет тенантов и обходит их по одному,
открывая на каждого обычную транзакцию с установленным тенантом. Тех же прав,
что у HTTP-запроса, и ни правом больше.

Цена решения — одна транзакция на тенанта за цикл. На пилоте это единицы
транзакций; когда тенантов станут сотни, встанет вопрос об отдельной роли
для фоновых задач, и это будет отдельное решение с ADR.

**Сбой одного тенанта не останавливает остальных.** Иначе один клиент
с испорченными учётными данными перевозчика заморозил бы трекинг всей
платформы — а сбой такого рода незаметен: статусы просто перестают
обновляться.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from aerogram.config import get_settings
from aerogram.core.models import Tenant
from aerogram.db import session_scope
from aerogram.intelligence.service import ScoreService
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import TenantStatus
from aerogram.shared.logging import get_logger
from aerogram.shipments.repository import ShipmentRepository
from aerogram.shipments.service import ShipmentService
from aerogram.worker.app import app

__all__ = [
    "SCORE_PERIOD_DAYS",
    "poll_shipment_statuses",
    "recalculate_carrier_score",
    "reconcile_ghost_shipments",
]

log = get_logger(__name__)

#: За какой период считается скор. Тридцать суток — скользящее окно
#: (системное ТЗ, раздел 11): более старые доставки говорят о перевозчике,
#: которого уже нет, а более короткое окно не набирает выборки.
SCORE_PERIOD_DAYS = 30

#: Сколько отправлений опрашивается за один проход по тенанту. Ограничение
#: нужно, чтобы один крупный клиент не занял весь цикл: неопрошенные вернутся
#: в очередь на следующей минуте, они никуда не денутся.
POLL_BATCH = 200


async def _active_tenants() -> list[UUID]:
    """Тенанты, которых имеет смысл обслуживать.

    Приостановленный тенант исключается: платформа не должна ходить
    к перевозчикам за того, кто не оплатил.
    """
    async with session_scope() as session:
        rows = await session.execute(
            select(Tenant.id).where(Tenant.status.in_([TenantStatus.ACTIVE, TenantStatus.TRIAL]))
        )
        return list(rows.scalars())


async def _for_each_tenant(name: str, action: Callable[[UUID], Awaitable[int]]) -> dict[str, int]:
    """Выполнить действие по каждому тенанту, не роняя цикл на одном из них."""
    handled = 0
    failed = 0
    for tenant_id in await _active_tenants():
        try:
            handled += await action(tenant_id)
        except Exception as exc:
            # Логируем тип, а не текст: в сообщении исключения может оказаться
            # шифротекст учётных данных или персональные данные адреса.
            failed += 1
            log.error(
                f"{name}.tenant_failed",
                tenant_id=str(tenant_id),
                error_type=type(exc).__name__,
            )
    log.info(name, handled=handled, failed_tenants=failed)
    return {"handled": handled, "failed_tenants": failed}


async def _poll_tenant(tenant_id: UUID) -> int:
    """Опросить перевозчиков по отправлениям, которым подошёл срок (FR-3.2)."""
    settings = get_settings()
    async with session_scope(tenant_id) as session:
        due = await ShipmentRepository(session).due_for_poll(POLL_BATCH)
        service = ShipmentService(session, settings)
        events = 0
        for shipment in due:
            try:
                events += await service.poll(shipment)
            except Exception as exc:
                # Сбой по одному отправлению не должен лишать обновлений
                # остальные: у них может быть другой перевозчик.
                log.warning(
                    "poll_shipment_statuses.shipment_failed",
                    number=shipment.number,
                    error_type=type(exc).__name__,
                )
        return events


async def _reconcile_tenant(tenant_id: UUID) -> int:
    """Догнать черновики, чей ответ на создание не дошёл (FR-2.5)."""
    async with session_scope(tenant_id) as session:
        return await ShipmentService(session, get_settings()).reconcile_unconfirmed(
            tenant_id=tenant_id
        )


async def _score_tenant(tenant_id: UUID) -> int:
    """Пересчитать скор по наблюдениям тенанта (FR-7.1).

    Снапшот платформенный, поэтому пересчёт по каждому тенанту подряд
    затирает предыдущий: это **не** свод по платформе, которого требует
    раздел 10.2, а его временная замена. Свод разбирается отдельно —
    см. docs/status.md.
    """
    today = utcnow().date()
    async with session_scope(tenant_id) as session:
        snapshots = await ScoreService(session).recalculate(
            today - timedelta(days=SCORE_PERIOD_DAYS), today
        )
        return len(snapshots)


# ``app.task`` для mypy нетипизирован: Celery не поставляет аннотаций, а пакет
# заглушек — новая зависимость, то есть решение человека (CLAUDE.md §2).
# ``warn_unused_ignores`` сторожит, чтобы эти подавления не остались мусором.
@app.task(name="aerogram.worker.tasks.poll_shipment_statuses")  # type: ignore[untyped-decorator]
def poll_shipment_statuses() -> dict[str, int]:
    """Опрос статусов по расписанию, ежеминутно."""
    return asyncio.run(_for_each_tenant("poll_shipment_statuses", _poll_tenant))


@app.task(name="aerogram.worker.tasks.reconcile_ghost_shipments")  # type: ignore[untyped-decorator]
def reconcile_ghost_shipments() -> dict[str, int]:
    """Сверка «призраков»: заказ у перевозчика есть, записи у нас нет."""
    return asyncio.run(_for_each_tenant("reconcile_ghost_shipments", _reconcile_tenant))


@app.task(name="aerogram.worker.tasks.recalculate_carrier_score")  # type: ignore[untyped-decorator]
def recalculate_carrier_score() -> dict[str, int]:
    """Ежесуточный пересчёт Carrier Score."""
    return asyncio.run(_for_each_tenant("recalculate_carrier_score", _score_tenant))
