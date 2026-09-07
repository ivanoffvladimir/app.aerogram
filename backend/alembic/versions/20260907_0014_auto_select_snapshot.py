"""Вердикт политики замораживается в расчёте, автовыбор доходит до решения.

ADR-0029. Правила маршрутизации применяются ПРИ РАСЧЁТЕ — все их условия
свойства запроса, — а решение принимается позже, отдельным запросом
по ``quote_id``. Чтобы понять, какое ``auto_select``-правило совпало,
рекомендации нужны те же факты запроса.

Восстанавливать их в ``routing`` нельзя, и это не вкусовщина. Двух полей
из шести нет в снимке запроса по контракту: ``Address`` намеренно без ФИАС.
Значит перевывод — это повторное разрешение города, а оно при промахе
локального справочника уходит к ДаData и **записывает** найденный город
в таблицу ``cities``. Таблица общая и **под RLS не находится** (миграция
0002). Город, появившийся между расчётом и рекомендацией — в том числе
от запроса чужого тенанта, — включил бы правило с условием ``direction``,
которого при расчёте не было. Отпечаток запроса это не ловит: разрешённых
идентификаторов в нём нет вовсе.

Поэтому вердикт вычисляется один раз там, где факты уже есть, и замораживается
в строке ``rate_quotes``:

* ``policy_version`` — отпечаток набора правил. Сегодня он растворяется внутри
  необратимого ``hash`` и наружу не виден, а рекомендация обязана назвать
  политику, которая ЭТОТ расчёт и порождала;
* ``policy_snapshot`` — факты запроса с уже разрешёнными городами, выбранное
  правило автовыбора и требование страхования.

Снимок решения получает четыре колонки, а не JSONB: по правилу фильтруют
(«решения, принятые правилом X»), а то, по чему фильтруют, живёт в колонке
(CLAUDE.md §6). Имя правила хранится РЯДОМ с идентификатором, потому что
переименование правила не должно переписывать историю, а удаление правила
не должно её стирать — внешнего ключа на ``routing_rules`` здесь нет
намеренно: ``RESTRICT`` запер бы удаление правила, ``SET NULL`` стёр бы
снимок.

**Функция неизменяемости переопределяется.** Четыре новые колонки — часть
снимка решения, и не назвать их в теле ``decisions_are_immutable()`` значило
бы оставить снимок неизменяемым во всём, кроме самого нового. Правило
на будущее: любая новая колонка ``decisions`` дописывается в эту функцию
той же миграцией.

Дифф чисто аддитивный: новых таблиц нет, RLS и ``tenant_id`` на обеих
таблицах уже действуют, миграции 0001–0013 не трогаются. ``downgrade``
восстанавливает тело функции из 0007 дословно.

Revision ID: 0014_auto_select_snapshot
Revises: 0013_platform_baseline
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_auto_select_snapshot"
down_revision: str | None = "0013_platform_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Колонки снимка автовыбора в решении. Либо все четыре, либо ни одной:
#: полуснимок не объясняет выбор и не годится ни для аналитики, ни для спора
#: с клиентом.
_SELECTION_COLUMNS = (
    "selection_rule",
    "auto_select_rule_id",
    "auto_select_rule_name",
    "selection_version",
)

#: Тело функции неизменяемости ПОСЛЕ миграции: пять полей из 0007 плюс
#: четыре новых.
_IMMUTABLE_WITH_SELECTION = """
CREATE OR REPLACE FUNCTION decisions_are_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.recommendation_id IS DISTINCT FROM OLD.recommendation_id
       OR NEW.selected_offer_id IS DISTINCT FROM OLD.selected_offer_id
       OR NEW.mode IS DISTINCT FROM OLD.mode
       OR NEW.override IS DISTINCT FROM OLD.override
       OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
       OR NEW.selection_rule IS DISTINCT FROM OLD.selection_rule
       OR NEW.auto_select_rule_id IS DISTINCT FROM OLD.auto_select_rule_id
       OR NEW.auto_select_rule_name IS DISTINCT FROM OLD.auto_select_rule_name
       OR NEW.selection_version IS DISTINCT FROM OLD.selection_version THEN
        RAISE EXCEPTION
            'снимок решения неизменяем: правка решения % запрещена', OLD.id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql
"""

#: Тело функции из миграции 0007, дословно. Нужно откату: оставить после
#: downgrade функцию, ссылающуюся на удалённые колонки, значит сломать
#: любое обновление решения.
_IMMUTABLE_WITHOUT_SELECTION = """
CREATE OR REPLACE FUNCTION decisions_are_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.recommendation_id IS DISTINCT FROM OLD.recommendation_id
       OR NEW.selected_offer_id IS DISTINCT FROM OLD.selected_offer_id
       OR NEW.mode IS DISTINCT FROM OLD.mode
       OR NEW.override IS DISTINCT FROM OLD.override
       OR NEW.decided_at IS DISTINCT FROM OLD.decided_at THEN
        RAISE EXCEPTION
            'снимок решения неизменяем: правка решения % запрещена', OLD.id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    # --- расчёт: замороженный вердикт политики ---
    op.add_column("rate_quotes", sa.Column("policy_version", sa.String(length=40), nullable=True))
    op.add_column(
        "rate_quotes",
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # --- решение: чем именно сделан автоматический выбор ---
    op.add_column("decisions", sa.Column("selection_rule", sa.String(length=30), nullable=True))
    op.add_column(
        "decisions",
        sa.Column("auto_select_rule_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "decisions", sa.Column("auto_select_rule_name", sa.String(length=255), nullable=True)
    )
    op.add_column("decisions", sa.Column("selection_version", sa.String(length=40), nullable=True))

    op.create_check_constraint(
        "only_auto_decisions_name_a_selection_rule",
        "decisions",
        "selection_rule IS NULL OR mode = 'auto'",
    )
    op.create_check_constraint(
        "auto_selection_is_whole",
        "decisions",
        f"num_nonnulls({', '.join(_SELECTION_COLUMNS)}) IN (0, 4)",
    )
    # По правилу фильтруют — «покажи решения, принятые правилом X», — значит
    # это индекс, а не украшение. Тенант первым: RLS всё равно сузит выборку
    # им же, и составной индекс отработает целиком.
    op.create_index(
        "ix_decisions_tenant_id_auto_select_rule_id",
        "decisions",
        ["tenant_id", "auto_select_rule_id"],
    )

    op.execute(_IMMUTABLE_WITH_SELECTION)


def downgrade() -> None:
    # Функция возвращается к телу 0007 ДО удаления колонок: иначе между
    # DROP COLUMN и переопределением остаётся окно, в котором триггер
    # ссылается на несуществующее поле.
    op.execute(_IMMUTABLE_WITHOUT_SELECTION)

    op.drop_index("ix_decisions_tenant_id_auto_select_rule_id", table_name="decisions")
    op.drop_constraint(op.f("ck_decisions_auto_selection_is_whole"), "decisions", type_="check")
    op.drop_constraint(
        op.f("ck_decisions_only_auto_decisions_name_a_selection_rule"), "decisions", type_="check"
    )
    for column in reversed(_SELECTION_COLUMNS):
        op.drop_column("decisions", column)

    op.drop_column("rate_quotes", "policy_snapshot")
    op.drop_column("rate_quotes", "policy_version")
