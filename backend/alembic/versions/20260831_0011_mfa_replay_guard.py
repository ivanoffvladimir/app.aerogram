"""Защита от повторного использования кода TOTP.

Код TOTP действует тридцать секунд, и в это окно его можно предъявить дважды:
подсмотренный через плечо или перехваченный код остаётся годным до конца шага.
RFC 6238 требует от проверяющей стороны не принимать один и тот же шаг дважды.

Хранится номер последнего принятого шага, а не сам код: код — секрет, а номер
шага секретом не является и сравнивается арифметически.

Колонка, а не Redis: Redis в пути аутентификации означал бы новый режим отказа —
при его недоступности пришлось бы либо пускать без защиты от повтора, либо
не пускать вовсе. Колонка живёт в той же транзакции, что и сам вход.
Обоснование — ADR-0018.

Revision ID: 0011_mfa_replay_guard
Revises: 0010_webhook_lookup
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_mfa_replay_guard"
down_revision: str | None = "0010_webhook_lookup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL означает «второй фактор ещё ни разу не предъявляли»: первый же код
    # принимается, любой следующий обязан быть строго новее.
    op.add_column("users", sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "mfa_last_step")
