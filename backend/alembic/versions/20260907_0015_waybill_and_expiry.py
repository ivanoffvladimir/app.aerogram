"""Номер накладной у отправления и истечение файла печатной формы.

ADR-0030 в редакции от 7 сентября 2026. Две вещи, обе — следствие того, что
при договоре на информационное обслуживание платформа ведёт **базу
электронных накладных**, а не архив документов.

**Номер накладной.** База накладных обязана знать номер самой накладной,
а мы его теряли: Деловые Линии возвращают идентификатор накладной
в ``LabelResult.external_ref``, адаптер его заполняет — и записать было
некуда. Колонка заводится у ОТПРАВЛЕНИЯ, а не у документа: накладная
принадлежит отправлению, и номер обязан пережить удаление файла, ради
которого вторая половина этой миграции и делается.

**Истечение файла.** Файл печатной формы — рабочий инструмент кладовщика,
а не наш архив: в роли информационного обслуживания перевозочные документы
хранят стороны договора перевозки. При этом в файле ФИО, адрес и телефон
получателя, а статья 5 закона № 152-ФЗ хранить их дольше цели не разрешает.
Значит файл обязан истекать, а запись о нём — оставаться: «этикетка была
заказана и напечатана» это факт каталога.

Отсюда четвёртое состояние документа — ``expired``. Не ``failed``: тот
означает «не получилось», а здесь получилось и отслужило. Спутать их значит
через год объяснять клиенту, что заказ сорвался, тогда как он был доставлен.

Ни одна строка не удаляется: сторож ``tests/unit/test_retention_guard.py``
на удаление архивной строки краснеет, и правильно делает.

Revision ID: 0015_waybill_and_expiry

Идентификатор короткий не для красоты: ``alembic_version.version_num`` —
``varchar(32)``, и длинное имя не записывается вовсе.
Revises: 0014_auto_select_snapshot
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_waybill_and_expiry"
down_revision: str | None = "0014_auto_select_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Состояния документа ПОСЛЕ миграции.
_STATUSES = ("pending", "ready", "failed", "expired")

#: Состояния до неё — нужны откату.
_STATUSES_BEFORE = ("pending", "ready", "failed")


def _status_check(values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"status IN ({listed})"


def upgrade() -> None:
    # --- номер накладной у отправления ---
    op.add_column("shipments", sa.Column("waybill_number", sa.String(length=64), nullable=True))

    # --- истёкший файл печатной формы ---
    op.drop_constraint(op.f("ck_documents_document_status"), "documents", type_="check")
    op.create_check_constraint("document_status", "documents", _status_check(_STATUSES))


def downgrade() -> None:
    # Истёкшие документы возвращаются в ``failed``: иначе ограничение
    # не даст откатиться, а терять запись о них нельзя — файла и так уже
    # нет, и строка осталась единственным следом.
    op.execute("UPDATE documents SET status = 'failed' WHERE status = 'expired'")
    op.drop_constraint(op.f("ck_documents_document_status"), "documents", type_="check")
    op.create_check_constraint("document_status", "documents", _status_check(_STATUSES_BEFORE))

    op.drop_column("shipments", "waybill_number")
