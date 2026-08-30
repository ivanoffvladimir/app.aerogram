"""Завести или обновить учётные данные перевозчика у тенанта.

Учётные данные перевозчика не живут ни в репозитории, ни в переменных
окружения: у каждого тенанта свой договор, и хранятся они зашифрованными
в ``carrier_accounts.credentials_encrypted`` (ADR-0005). Кабинета для этого
пока нет, поэтому — скрипт.

Значения читаются с ввода, а не из аргументов командной строки: аргументы
видны в ``ps`` и остаются в истории оболочки.

    uv run python scripts/set_carrier_credentials.py --tenant <uuid> --carrier major

В интерактивном режиме скрипт спросит логин и пароль (ввод не отображается).
Для автоматизации на вход подаётся JSON-объект:

    uv run python scripts/set_carrier_credentials.py --tenant <uuid> --carrier major < creds.json

Файл с учётными данными после этого удаляется, а до того держится с правами 600.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from getpass import getpass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.carriers.credentials import PENDING_CARRIERS, schema_for
from aerogram.config import get_settings
from aerogram.core.models import CarrierAccount
from aerogram.directories.models import Carrier
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.ids import uuid7


def _fields_for(carrier_code: str, override: str | None) -> tuple[str, ...]:
    """Какие поля спрашивать: из описания перевозчика или заданные явно."""
    if override:
        return tuple(name.strip() for name in override.split(",") if name.strip())

    schema = schema_for(carrier_code)
    if schema is not None:
        return schema.names
    if carrier_code in PENDING_CARRIERS:
        raise SystemExit(
            f"состав доступов {carrier_code} ещё не определён — он появится вместе "
            "с адаптером; до тех пор перечислите поля явно: --fields login,password"
        )
    raise SystemExit(
        f"неизвестный перевозчик {carrier_code!r}: перечислите поля явно, например --fields api_key"
    )


def _check(carrier_code: str, fields: tuple[str, ...], values: dict[str, str]) -> None:
    """Сверить состав. Опечатка в имени поля иначе всплыла бы только у перевозчика.

    В сообщение попадают только ИМЕНА полей: значения не должны оказаться
    ни в выводе, ни в журнале сеанса.
    """
    missing = [name for name in fields if not values.get(name)]
    if missing:
        raise SystemExit("не заполнены поля: " + ", ".join(missing))
    if schema_for(carrier_code) is not None:
        extra = sorted(set(values) - set(fields))
        if extra:
            raise SystemExit(
                "лишние поля: " + ", ".join(extra) + "; ожидаются: " + ", ".join(fields)
            )


def _read_credentials(carrier_code: str, fields: tuple[str, ...]) -> dict[str, str]:
    """Считать значения: с клавиатуры или из JSON на стандартном вводе."""
    if not sys.stdin.isatty():
        parsed = json.load(sys.stdin)
        if not isinstance(parsed, dict):
            raise SystemExit('на вход ожидается JSON-объект вида {"login": "..."}')
        values = {str(k): str(v) for k, v in parsed.items()}
        _check(carrier_code, fields, values)
        return values

    schema = schema_for(carrier_code)
    if schema is not None:
        print(schema.where_to_get)
    labels = {f.name: f.label for f in schema.fields} if schema else {}

    values = {}
    for field in fields:
        value = getpass(f"{labels.get(field, field)}: ")
        if not value:
            raise SystemExit(f"поле {field} не может быть пустым")
        values[field] = value
    return values


async def _store(tenant_id: UUID, carrier_code: str, credentials: dict[str, str], mode: str) -> str:
    settings = get_settings()
    engine = create_async_engine(str(settings.database_migration_url or settings.database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cipher = CredentialCipher(settings.credential_key_map, settings.credential_active_key_id)

    try:
        async with factory() as db, db.begin():
            carrier = (
                await db.execute(select(Carrier).where(Carrier.code == carrier_code))
            ).scalar_one_or_none()
            if carrier is None:
                raise SystemExit(f"перевозчика с кодом {carrier_code!r} нет в справочнике")

            # RLS: без установленного тенанта строка не найдётся и не запишется.
            await db.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            account = (
                await db.execute(
                    select(CarrierAccount).where(CarrierAccount.carrier_id == carrier.id)
                )
            ).scalar_one_or_none()

            if account is None:
                account = CarrierAccount(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    carrier_id=carrier.id,
                    mode=mode,
                    credentials_encrypted="",
                    is_active=True,
                    status="unchecked",
                )
                db.add(account)
                await db.flush()
                action = "создана"
            else:
                account.mode = mode
                action = "обновлена"

            # Шифротекст привязан к идентификатору записи: перенос его в чужую
            # строку не расшифруется.
            account.credentials_encrypted = cipher.encrypt(
                json.dumps(credentials), aad=str(account.id).encode()
            )
            # Прежний вердикт проверки больше не относится к новым данным.
            account.status = "unchecked"
            account.status_message = None
    finally:
        await engine.dispose()
    return action


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, type=UUID, help="идентификатор тенанта")
    parser.add_argument("--carrier", required=True, help="код перевозчика, например major")
    parser.add_argument(
        "--fields",
        default=None,
        help="имена полей через запятую; по умолчанию берутся из carriers/credentials.py",
    )
    parser.add_argument(
        "--mode",
        default="own_contract",
        choices=("own_contract", "aerogram"),
        help="own_contract — договор клиента с перевозчиком (по умолчанию), "
        "aerogram — тариф платформы",
    )
    args = parser.parse_args()

    fields = _fields_for(args.carrier, args.fields)
    credentials = _read_credentials(args.carrier, fields)

    action = asyncio.run(_store(args.tenant, args.carrier, credentials, args.mode))
    # Печатаются только имена полей: значения не должны попасть ни в вывод,
    # ни в журнал сеанса.
    print(f"Учётная запись {args.carrier} для тенанта {args.tenant} {action}.")
    print("Записаны поля: " + ", ".join(sorted(credentials)))


if __name__ == "__main__":
    main()
