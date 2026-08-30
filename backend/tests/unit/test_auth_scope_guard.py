"""Сторож окон аутентификации.

Две политики открывают узкое окно поверх RLS, когда тенант ещё неизвестен:
``users_auth_lookup`` (миграция 0002) на чтение ``users`` при входе и
``api_keys_auth_lookup`` (миграция 0008) на чтение ``api_keys``. Компромисс
приемлем ровно до тех пор, пока окна открываются в одном модуле —
``core/service.py``, методы ``AuthService._lookup_user``
и ``ApiKeyService._lookup_key``.

Этот тест следит, чтобы третье место не появилось незаметно. Обоснование — ADR-0004.
"""

from __future__ import annotations

from pathlib import Path

from aerogram.core.service import AUTH_SCOPE_SETTING

#: Файлы, которым разрешено упоминать настройку окна.
ALLOWED = frozenset({"service.py"})


def test_auth_scope_is_set_in_exactly_one_module(source_files: list[Path]) -> None:
    offenders = [
        path
        for path in source_files
        if AUTH_SCOPE_SETTING in path.read_text(encoding="utf-8") and path.name not in ALLOWED
    ]
    assert not offenders, (
        "окно поиска пользователя при входе должно открываться только в "
        f"core/service.py, найдено также в: {[str(p) for p in offenders]}"
    )


def test_setting_name_is_namespaced() -> None:
    # Настройка должна лежать в пространстве app.*, иначе её не отличить от
    # параметров самого PostgreSQL.
    assert AUTH_SCOPE_SETTING.startswith("app.")
