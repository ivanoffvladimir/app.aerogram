"""Эндпоинты сверки расходов (экран `/invoices`)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from aerogram.billing.schemas import ReconciliationOut, ReconciliationState
from aerogram.billing.service import DEFAULT_DAYS, MAX_DAYS, BillingService
from aerogram.core.deps import SessionDep, require_roles
from aerogram.shared.enums import UserRole

__all__ = ["billing_router"]

billing_router = APIRouter(prefix="/billing", tags=["Расходы"])

#: Финансы видят владелец и логист. Во фронт-ТЗ это отдельное право
#: `billing.view`, но словаря прав в системе нет — есть роли, и отображение
#: права на роли было бы продуктовым решением. Взято самое узкое из
#: осмысленных: оператор оформляет отправления, а не разбирает счета.
_CAN_VIEW = require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)


@billing_router.get(
    "/reconciliation",
    response_model=ReconciliationOut,
    summary="Сверка расчёта и счетов",
)
async def reconciliation(
    principal: Annotated[object, _CAN_VIEW],
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
    carrier_id: UUID | None = None,
    state: ReconciliationState | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ReconciliationOut:
    """Что обещал расчёт, что выставил перевозчик и где разошлось.

    Отправление без счёта попадает в `awaiting`, а не в «сошлось»: пустота
    не равна совпадению, и разница здесь — это деньги клиента.
    """
    return await BillingService(session).reconciliation(
        days=days,
        carrier_id=carrier_id,
        state=state.value if state is not None else None,
        page=page,
        page_size=page_size,
    )
