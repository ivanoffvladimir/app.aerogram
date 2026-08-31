"""Эндпоинты сводки кабинета."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from aerogram.core.deps import CurrentPrincipal, SessionDep
from aerogram.reports.schemas import SummaryOut
from aerogram.reports.service import DEFAULT_DAYS, MAX_DAYS, ReportService

__all__ = ["reports_router"]

reports_router = APIRouter(prefix="/reports", tags=["Отчёты"])


@reports_router.get("/summary", response_model=SummaryOut, summary="Сводка кабинета")
async def summary(
    principal: CurrentPrincipal,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> SummaryOut:
    """SLA, расходы, решения и открытые исключения.

    Экономии в сводке нет: база сравнения — решение человека, а не умолчание
    разработчика (см. модуль сервиса и docs/status.md).
    """
    return await ReportService(session).summary(days)
