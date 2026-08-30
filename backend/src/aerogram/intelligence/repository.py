"""Репозиторий Carrier Intelligence. Единственное место с SQL в модуле.

Читает домен и ничего в нём не меняет: модуль работает только на чтение
(CLAUDE.md §4, пункт 4). Пишет он лишь собственные снапшоты.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.intelligence.models import CarrierScoreSnapshot
from aerogram.shared.enums import ScoreScope, ShipmentStatus
from aerogram.shipments.models import Shipment
from aerogram.tracking.models import DeliveryOutcome, ShipmentEvent

__all__ = ["Observations", "ScoreRepository"]

#: Сколько событий трекинга считается «прозрачным» перевозчиком (раздел 10.1).
MIN_EVENTS_FOR_QUALITY = 3


@dataclass(frozen=True, slots=True)
class Observations:
    """Сырые счётчики по одному перевозчику за период.

    Доли из них считает формула: держать здесь уже поделённые значения
    значило бы прятать размер выборки, а он определяет доверие.
    """

    carrier_id: UUID
    finalized: int
    with_deadline: int
    on_time: int
    broken: int
    with_incident: int
    transparent: int
    median_cost_minor: int | None


class ScoreRepository:
    """Наблюдения по домену и снапшоты скора."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def observations(self, period_start: date, period_end: date) -> list[Observations]:
        """Счётчики по каждому перевозчику за период.

        В выборку входят только завершённые отправления: незавершённое ничего
        не говорит о перевозчике — оно ещё может быть доставлено вовремя.
        Отменённое говорит, поэтому оно в выборке есть.

        Видимость определяется RLS: под ролью приложения это наблюдения
        одного тенанта. Платформенный свод по всем тенантам сразу требует
        отдельного решения по доступу — см. docs/status.md.
        """
        events = (
            select(ShipmentEvent.shipment_id, func.count().label("n"))
            .group_by(ShipmentEvent.shipment_id)
            .subquery()
        )
        stmt = (
            select(
                Shipment.carrier_id,
                func.count().label("finalized"),
                func.count(DeliveryOutcome.deadline_met).label("with_deadline"),
                func.count(1).filter(DeliveryOutcome.deadline_met.is_(True)).label("on_time"),
                func.count(1)
                .filter(
                    (Shipment.status == ShipmentStatus.CANCELLED)
                    | (Shipment.status == ShipmentStatus.RETURNED)
                    | (Shipment.incident_type.is_not(None))
                )
                .label("broken"),
                func.count(1).filter(Shipment.has_incident.is_(True)).label("with_incident"),
                func.count(1)
                .filter(
                    (Shipment.status == ShipmentStatus.DELIVERED)
                    & (func.coalesce(events.c.n, 0) >= MIN_EVENTS_FOR_QUALITY)
                )
                .label("transparent"),
                func.percentile_cont(0.5)
                .within_group(Shipment.price_quoted_amount_minor)
                .label("median_cost"),
            )
            .select_from(Shipment)
            .outerjoin(DeliveryOutcome, DeliveryOutcome.shipment_id == Shipment.id)
            .outerjoin(events, events.c.shipment_id == Shipment.id)
            .where(
                Shipment.status.in_(
                    [ShipmentStatus.DELIVERED, ShipmentStatus.RETURNED, ShipmentStatus.CANCELLED]
                ),
                func.date(Shipment.created_at) >= period_start,
                func.date(Shipment.created_at) <= period_end,
            )
            .group_by(Shipment.carrier_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            Observations(
                carrier_id=row.carrier_id,
                finalized=row.finalized,
                with_deadline=row.with_deadline,
                on_time=row.on_time,
                broken=row.broken,
                with_incident=row.with_incident,
                transparent=row.transparent,
                median_cost_minor=int(row.median_cost) if row.median_cost is not None else None,
            )
            for row in rows
        ]

    async def latest(
        self, carrier_id: UUID, scope_type: ScoreScope, scope_key: str = ""
    ) -> CarrierScoreSnapshot | None:
        """Самый свежий снапшот в заданном разрезе."""
        stmt = (
            select(CarrierScoreSnapshot)
            .where(
                CarrierScoreSnapshot.carrier_id == carrier_id,
                CarrierScoreSnapshot.scope_type == scope_type,
                CarrierScoreSnapshot.scope_key == scope_key,
            )
            .order_by(
                CarrierScoreSnapshot.period_end.desc(),
                CarrierScoreSnapshot.calculated_at.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def upsert(self, snapshot: CarrierScoreSnapshot) -> CarrierScoreSnapshot:
        """Записать снапшот, заменив пересчёт того же периода и той же версии.

        Ключ уникальности включает версию формулы: изменение весов создаёт
        новый снапшот рядом со старым, а не переписывает историю (FR-7.4).
        """
        existing = (
            (
                await self._session.execute(
                    select(CarrierScoreSnapshot).where(
                        CarrierScoreSnapshot.carrier_id == snapshot.carrier_id,
                        CarrierScoreSnapshot.scope_type == snapshot.scope_type,
                        CarrierScoreSnapshot.scope_key == snapshot.scope_key,
                        CarrierScoreSnapshot.period_start == snapshot.period_start,
                        CarrierScoreSnapshot.period_end == snapshot.period_end,
                        CarrierScoreSnapshot.formula_version == snapshot.formula_version,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            self._session.add(snapshot)
            return snapshot

        for field in (
            "sample_size",
            "on_time_rate",
            "avg_delay_days",
            "reliability",
            "incident_rate",
            "price_index",
            "data_quality",
            "score",
            "confidence",
        ):
            setattr(existing, field, getattr(snapshot, field))
        return existing
