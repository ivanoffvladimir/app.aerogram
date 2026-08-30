"""Починка замороженных значений по умолчанию у колонок времени.

Revision ID: 0005_fix_frozen_defaults
Revises: 0004_directories_week3

Идентификатор ревизии короткий не случайно: колонка ``alembic_version.version_num``
объявлена как ``varchar(32)``, и более длинное имя роняет саму миграцию.

ЧТО СЛОМАНО. В миграции 0001 восемь колонок получили ``server_default``,
заданный ПИТОНОВСКОЙ СТРОКОЙ ``"now()"``, а не ``sa.text("now()")``. Alembic
отрендерил это как строковый литерал, PostgreSQL разобрал его как ввод типа
timestamptz и **вычислил один раз в момент выполнения миграции**. В результате
в схеме оказалось не ``DEFAULT now()``, а ``DEFAULT '2026-08-30 04:48:47+00'``.

ЧЕМ ЭТО ГРОЗИТ. Любая вставка без явного значения получает не текущее время,
а момент выполнения миграции. Проверено: две записи в ``audit_log`` с разницей
в секунду получили одинаковый штамп, отличающийся от ``now()`` на 36 секунд.
Тяжелее всего это бьёт по:

* ``audit_log.created_at`` — аудит изменяющих операций (12.6 ТЗ) перестаёт
  быть доказательством: все записи с одним временем;
* ``shipment_events.received_at`` — разница между ``occurred_at``
  и ``received_at`` это и есть измеряемая задержка перевозчика;
* ``carrier_raw_calls.created_at`` — по нему чистится сырьё старше 30 суток.

Ошибка не проявлялась в тестах: модели с ``TimestampMixin`` проставляют время
на стороне Python, а перечисленные восемь колонок объявлены отдельно и
питоновского значения по умолчанию не имели.

ЧТО СДЕЛАНО. Миграция 0001 не правится (CLAUDE.md §8) — исправление вынесено
отдельной ревизией. Параллельно всем восьми колонкам добавлено питоновское
``default=utcnow``: приложение больше не зависит от того, верен ли DEFAULT
в базе. Регрессия закрыта тестом ``TestTimestampDefaults``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_fix_frozen_defaults"
down_revision: str | None = "0004_directories_week3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Колонки, у которых значение по умолчанию оказалось замороженным литералом.
FROZEN_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("audit_log", "created_at"),
    ("carrier_raw_calls", "created_at"),
    ("carrier_score_snapshots", "calculated_at"),
    ("documents", "created_at"),
    ("rate_quotes", "created_at"),
    ("rate_requests", "created_at"),
    ("shipment_events", "received_at"),
    ("webhook_deliveries", "created_at"),
)


def upgrade() -> None:
    for table, column in FROZEN_DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT now()")


def downgrade() -> None:
    """Значение по умолчанию снимается, а не восстанавливается.

    Прежнее состояние — литерал с моментом выполнения миграции 0001 —
    воспроизвести невозможно и не нужно: это и была ошибка. Снятие DEFAULT
    безопасно, потому что все восемь колонок теперь получают значение
    на стороне приложения.
    """
    for table, column in FROZEN_DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
