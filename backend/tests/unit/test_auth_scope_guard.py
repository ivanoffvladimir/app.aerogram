"""Сторож окон аутентификации.

Три политики открывают узкое окно поверх RLS, когда тенант ещё неизвестен:

* ``users_auth_lookup`` (миграция 0002) — чтение ``users`` при входе;
* ``api_keys_auth_lookup`` (миграция 0008) — чтение ``api_keys`` по ключу;
* ``shipments_webhook_lookup`` (миграция 0010) — чтение ``shipments``
  по заказу перевозчика при приёме вебхука.

Компромисс приемлем ровно до тех пор, пока каждое окно открывается
в единственном месте. Этот тест следит, чтобы новое место не появилось
незаметно. Обоснование — ADR-0004 и ADR-0015.

Раньше здесь сверялось ИМЯ файла (``service.py``), то есть проверка
пропускала любой сервис любого модуля. Теперь сверяется путь: файл, а не
его название.

Проверка намеренно тупая: она ищет имя настройки в тексте файла, включая
комментарии и докстроки. Ослаблять её до «только настоящие вызовы» нельзя —
именно на догадках о том, что считать настоящим вызовом, такие сторожа
и перестают ловить. Цена: описывая механизм в прозе, имя настройки
приходится не называть дословно.
"""

from __future__ import annotations

from pathlib import Path

from aerogram.core.service import AUTH_SCOPE_SETTING

#: Файлы, которым разрешено упоминать настройку окна, — полными путями
#: относительно ``src/aerogram``.
ALLOWED = frozenset(
    {
        "core/service.py",
        "shipments/repository.py",
    }
)


def _relative(path: Path) -> str:
    parts = path.parts
    return "/".join(parts[parts.index("aerogram") + 1 :])


def test_auth_scope_is_set_only_where_allowed(source_files: list[Path]) -> None:
    offenders = [
        _relative(path)
        for path in source_files
        if AUTH_SCOPE_SETTING in path.read_text(encoding="utf-8") and _relative(path) not in ALLOWED
    ]
    assert not offenders, (
        "окно поиска поверх RLS открывается только в "
        f"{sorted(ALLOWED)}, найдено также в: {offenders}"
    )


def test_the_allowed_list_names_files_that_exist(source_files: list[Path]) -> None:
    """Иначе список разрешённых тихо перестанет что-либо разрешать.

    Файл переименовали, запись осталась — и сторож начинает пропускать
    настоящее нарушение, продолжая зеленеть.
    """
    known = {_relative(path) for path in source_files}
    assert known >= ALLOWED, f"нет таких файлов: {sorted(ALLOWED - known)}"


def test_every_allowed_file_actually_opens_a_window(source_files: list[Path]) -> None:
    """Разрешение, которым никто не пользуется, — забытое разрешение."""
    opens = {
        _relative(path)
        for path in source_files
        if AUTH_SCOPE_SETTING in path.read_text(encoding="utf-8")
    }
    unused = sorted(ALLOWED - opens)
    assert not unused, f"окно больше не открывается, убрать из списка: {unused}"


def test_setting_name_is_namespaced() -> None:
    # Настройка должна лежать в пространстве app.*, иначе её не отличить от
    # параметров самого PostgreSQL.
    assert AUTH_SCOPE_SETTING.startswith("app.")
