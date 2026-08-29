"""Приложение Celery: фоновые задачи и расписания.

Расписания заданы здесь, а не в crontab: так они версионируются вместе с кодом
и видны при ревью. Сами задачи появляются по мере готовности модулей.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from aerogram.config import get_settings
from aerogram.shared.logging import configure_logging

__all__ = ["app"]

settings = get_settings()
configure_logging(level=settings.log_level, json_output=settings.log_json)

app = Celery("aerogram", broker=settings.broker_url, backend=settings.result_backend)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Задача берётся из очереди только после подтверждения выполнения: падение
    # воркера не должно тихо терять создание отправления.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=270,
    result_expires=3600,
)

app.conf.beat_schedule = {
    # Сырьё вызовов перевозчиков хранится 30 суток (раздел 8.2 ТЗ, п. 6).
    "purge-raw-calls": {
        "task": "aerogram.worker.tasks.purge_raw_calls",
        "schedule": crontab(hour=3, minute=30),
    },
    # Адаптивный polling статусов (FR-3.2). Планировщик опирается на частичный
    # индекс по незавершённым отправлениям.
    "poll-shipment-statuses": {
        "task": "aerogram.worker.tasks.poll_shipment_statuses",
        "schedule": crontab(minute="*"),
    },
    # Справочники терминалов и ПВЗ синхронизируются ежесуточно (FR-8.3).
    "sync-carrier-references": {
        "task": "aerogram.worker.tasks.sync_carrier_references",
        "schedule": crontab(hour=2, minute=0),
    },
    # Контроль срока: список отправлений с риском срыва (С-4).
    "detect-delivery-risk": {
        "task": "aerogram.worker.tasks.detect_delivery_risk",
        "schedule": crontab(hour=5, minute=0),  # 08:00 по Москве
    },
    # Carrier Score пересчитывается ежесуточно (FR-7.1).
    "recalculate-carrier-score": {
        "task": "aerogram.worker.tasks.recalculate_carrier_score",
        "schedule": crontab(hour=4, minute=0),
    },
    # Утренний дайджест оператору (раздел 12 ТЗ).
    "daily-digest": {
        "task": "aerogram.worker.tasks.send_daily_digest",
        "schedule": crontab(hour=6, minute=0),
    },
}

# Задачи регистрируются автопоиском по модулю tasks, когда он появится.
app.autodiscover_tasks(["aerogram.worker"], related_name="tasks", force=False)
