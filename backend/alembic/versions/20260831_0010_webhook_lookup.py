"""Узкое окно на поиск отправления по вебхуку перевозчика.

Вебхук приходит БЕЗ контекста тенанта: перевозчик знает только свой
идентификатор заказа. Роль приложения работает под ``FORCE ROW LEVEL SECURITY``
и без установленного ``app.tenant_id`` не видит ни строки, поэтому найти
отправление обычным запросом невозможно.

Решение принято человеком и записано в ADR-0015: то же узкое окно, что уже
дважды применено — на поиск пользователя при входе (ADR-0004, миграция 0002)
и на поиск API-ключа (миграция 0008). Значение ``webhook`` отличается от
``login`` и ``api_key`` намеренно: окно на ``shipments`` не должно открывать
``users`` и наоборот.

Ограничения окна:

* только ``SELECT`` — записать через него нельзя, приём событий идёт уже
  под тенантом найденного отправления;
* только на время транзакции (``set_config(..., is_local => true)``);
* открывается в единственном месте — ``shipments.repository.find_for_webhook``,
  за чем следит ``tests/unit/test_auth_scope_guard.py``.

Revision ID: 0010_webhook_lookup
Revises: 0009_score_per_tenant
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_webhook_lookup"
down_revision: str | None = "0009_score_per_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "shipments_webhook_lookup"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON shipments
            FOR SELECT
            USING (current_setting('app.auth_scope', true) = 'webhook')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON shipments")
