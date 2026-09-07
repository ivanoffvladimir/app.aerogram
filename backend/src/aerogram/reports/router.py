"""Эндпоинты сводки кабинета."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from aerogram.core.deps import CurrentPrincipal, SessionDep
from aerogram.reports.schemas import SummaryOut
from aerogram.reports.service import DEFAULT_DAYS, MAX_DAYS, ReportService
from aerogram.shared.enums import UserRole

__all__ = ["reports_router"]

reports_router = APIRouter(prefix="/reports", tags=["Отчёты"])

#: Кому показываются расходы. Тот же круг, что у сверки со счетами
#: (`/v1/billing/reconciliation`), и по той же причине: оператор оформляет
#: отправления, а не разбирает счета.
#:
#: Правило существовало и раньше, но стояло только на сверке — а те же суммы
#: за тот же период отдавала сводка, без единой проверки роли. Правило,
#: которое обходится соседним путём, не правило.
#:
#: **Машинный клиент в круг входит.** Ограничение здесь про людей внутри
#: тенанта, а доступ интеграции решается областью ключа: `analytics:read`
#: выдана этому пути осознанно, и молча опустошать её ответ значило бы
#: сломать интеграцию проверкой, которая заводилась не про неё.
COSTS_ROLES = frozenset({UserRole.OWNER, UserRole.LOGISTICIAN, UserRole.API_CLIENT})


@reports_router.get("/summary", response_model=SummaryOut, summary="Сводка кабинета")
async def summary(
    principal: CurrentPrincipal,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> SummaryOut:
    """SLA, расходы, решения и открытые исключения.

    Сводка **не закрывается целиком**: соблюдение срока и открытые исключения
    нужны как раз оператору, и отнять у него весь экран ради одного раздела
    значило бы сломать его работу вместо того, чтобы закрыть деньги.

    Экономии в сводке нет: база сравнения — решение человека, а не умолчание
    разработчика (см. модуль сервиса и docs/status.md).
    """
    return await ReportService(session).summary(days, with_costs=principal.role in COSTS_ROLES)
