"""Окно поиска API-ключа поверх RLS.

Аутентификация по ключу была неработоспособна: тенант определяется ПО ключу,
поэтому на момент поиска ``app.tenant_id`` ещё не установлен, а политика
``tenant_isolation`` на ``api_keys`` объявлена с ``FORCE`` и не отдаёт ни одной
строки. Любой запрос с ``X-Api-Key`` получал «Ключ недействителен» независимо
от того, был ключ действителен или нет. Ни один тест этот путь не покрывал.

Решение — то же, что уже принято для поиска пользователя при входе (ADR-0004):
узкая политика только на SELECT, открываемая транзакционной настройкой
``app.auth_scope``. Значение отличается от логина намеренно: окно на ``users``
не должно открывать ``api_keys`` и наоборот.

Ограничения окна:

* только ``SELECT`` — записать через него нельзя;
* только на время транзакции (``set_config(..., is_local => true)``);
* открывается в единственном месте — ``core.service.ApiKeyService._lookup_key``,
  за чем следит ``tests/unit/test_auth_scope_guard.py``.

Revision ID: 0008_api_key_lookup
Revises: 0007_decision_engine
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_api_key_lookup"
down_revision: str | None = "0007_decision_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "api_keys_auth_lookup"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON api_keys
            FOR SELECT
            USING (current_setting('app.auth_scope', true) = 'api_key')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON api_keys")
