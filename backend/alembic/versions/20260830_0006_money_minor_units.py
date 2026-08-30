"""Деньги в минорных единицах (ADR-0011).

ТЗ v3 требует денежную модель ``BIGINT amount_minor + CHAR(3) currency``:
раздел 4 бэкенд-ТЗ, схема ``Money`` в ``docs/tz/v3/openapi.yaml``, раздел 10
фронт-ТЗ. До этой миграции деньги хранились как ``NUMERIC(12,2)`` по ADR-0006.

Пять денежных колонок и только они. Остальные ``NUMERIC`` в схеме — вес,
коэффициенты и доли; они деньгами не являются и не трогаются.

Валюта остаётся на уровне ``rate_quotes`` и ``shipments``. У ``shipment_items``
своей валюты нет намеренно: перевозчик выставляет счёт в одной валюте на всю
отправку, и вторая колонка означала бы возможность расхождения строки с шапкой.
Money для строки собирается из её суммы и валюты отправления.

Обратимость. ``downgrade`` делит на 100 и возвращает ``NUMERIC(12,2)``.
Для валют с двумя знаками потери нет; для валюты с иным числом знаков
преобразование было бы неверным, поэтому ``downgrade`` останавливается,
если в данных встретилась такая валюта.

Revision ID: 0006_money_minor_units
Revises: 0005_fix_frozen_defaults
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_money_minor_units"
down_revision: str | None = "0005_fix_frozen_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (таблица, старая колонка, новая колонка, обязательность).
MONEY_COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("rate_quotes", "price", "price_amount_minor", False),
    ("shipments", "declared_value", "declared_value_amount_minor", False),
    ("shipments", "price_quoted", "price_quoted_amount_minor", False),
    ("shipments", "price_actual", "price_actual_amount_minor", False),
    ("shipment_items", "price", "price_amount_minor", True),
)

#: Ограничения, ссылающиеся на переименованные колонки. Пересоздаются на новые.
CONSTRAINTS_TO_DROP: tuple[tuple[str, str], ...] = (
    ("rate_quotes", "ck_rate_quotes_quote_price_xor_error"),
    ("rate_quotes", "ck_rate_quotes_quote_price_non_negative"),
    ("shipments", "ck_shipments_shipment_price_quoted_non_negative"),
    ("shipment_items", "ck_shipment_items_item_price_non_negative"),
)

#: Валюты, у которых число знаков в минорной единице не равно двум.
#: Держится в синхронизации с ``shared.money._MINOR_UNIT_EXPONENTS``.
NON_TWO_DIGIT_CURRENCIES = ("JPY", "KRW", "CLP", "VND", "ISK", "BHD", "KWD", "OMR", "TND")


def upgrade() -> None:
    for table, name in CONSTRAINTS_TO_DROP:
        # op.f: имя уже окончательное, соглашение об именах применять к нему не нужно.
        op.drop_constraint(op.f(name), table, type_="check")

    for table, old, new, required in MONEY_COLUMNS:
        op.add_column(table, sa.Column(new, sa.BigInteger(), nullable=True))
        # round() до целого, а не усечение: 24099.999 это 24100 копеек.
        op.execute(f"UPDATE {table} SET {new} = round({old} * 100)::bigint WHERE {old} IS NOT NULL")
        if required:
            op.alter_column(table, new, nullable=False)
        op.drop_column(table, old)

    op.create_check_constraint(
        "quote_price_xor_error",
        "rate_quotes",
        "(price_amount_minor IS NOT NULL AND error_code IS NULL)"
        " OR (price_amount_minor IS NULL AND error_code IS NOT NULL)",
    )
    op.create_check_constraint(
        "quote_price_non_negative",
        "rate_quotes",
        "price_amount_minor IS NULL OR price_amount_minor >= 0",
    )
    op.create_check_constraint(
        "shipment_price_quoted_non_negative",
        "shipments",
        "price_quoted_amount_minor IS NULL OR price_quoted_amount_minor >= 0",
    )
    op.create_check_constraint(
        "shipment_price_actual_non_negative",
        "shipments",
        "price_actual_amount_minor IS NULL OR price_actual_amount_minor >= 0",
    )
    op.create_check_constraint(
        "shipment_declared_value_non_negative",
        "shipments",
        "declared_value_amount_minor IS NULL OR declared_value_amount_minor >= 0",
    )
    op.create_check_constraint(
        "item_price_non_negative", "shipment_items", "price_amount_minor >= 0"
    )

    # CHAR(3) вместо VARCHAR(3): код валюты ISO 4217 всегда ровно три знака,
    # и ТЗ v3 называет именно CHAR(3).
    for table in ("rate_quotes", "shipments"):
        op.alter_column(
            table,
            "currency",
            type_=sa.CHAR(3),
            existing_type=sa.String(3),
            existing_nullable=False,
        )
        op.create_check_constraint("currency_is_iso_4217", table, "currency ~ '^[A-Z]{3}$'")


def downgrade() -> None:
    currencies = ", ".join(f"'{code}'" for code in NON_TWO_DIGIT_CURRENCIES)
    for table in ("rate_quotes", "shipments"):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM {table} WHERE currency IN ({currencies})) THEN
                    RAISE EXCEPTION
                        'downgrade невозможен: в {table} есть суммы в валюте, '
                        'у которой число знаков в минорной единице не равно двум';
                END IF;
            END $$;
            """
        )

    for table in ("rate_quotes", "shipments"):
        op.drop_constraint(op.f(f"ck_{table}_currency_is_iso_4217"), table, type_="check")
        op.alter_column(
            table,
            "currency",
            type_=sa.String(3),
            existing_type=sa.CHAR(3),
            existing_nullable=False,
        )

    for name, table in (
        ("ck_rate_quotes_quote_price_xor_error", "rate_quotes"),
        ("ck_rate_quotes_quote_price_non_negative", "rate_quotes"),
        ("ck_shipments_shipment_price_quoted_non_negative", "shipments"),
        ("ck_shipments_shipment_price_actual_non_negative", "shipments"),
        ("ck_shipments_shipment_declared_value_non_negative", "shipments"),
        ("ck_shipment_items_item_price_non_negative", "shipment_items"),
    ):
        op.drop_constraint(op.f(name), table, type_="check")

    for table, old, new, required in MONEY_COLUMNS:
        op.add_column(table, sa.Column(old, sa.Numeric(precision=12, scale=2), nullable=True))
        op.execute(f"UPDATE {table} SET {old} = {new} / 100.0 WHERE {new} IS NOT NULL")
        if required:
            op.alter_column(table, old, nullable=False)
        op.drop_column(table, new)

    op.create_check_constraint(
        "quote_price_xor_error",
        "rate_quotes",
        "(price IS NOT NULL AND error_code IS NULL) OR (price IS NULL AND error_code IS NOT NULL)",
    )
    op.create_check_constraint(
        "quote_price_non_negative", "rate_quotes", "price IS NULL OR price >= 0"
    )
    op.create_check_constraint(
        "shipment_price_quoted_non_negative",
        "shipments",
        "price_quoted IS NULL OR price_quoted >= 0",
    )
    op.create_check_constraint("item_price_non_negative", "shipment_items", "price >= 0")
