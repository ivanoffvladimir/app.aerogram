"""Архив хранится не менее пяти лет, и это должно быть решением, а не удачей.

Транспортные и курьерские накладные — первичные документы, и срок хранения
у них не наш (ADR-0030). Сегодня он соблюдается по счастливой причине:
удалять отправления просто нечем. Такая гарантия держится ровно до первой
правки, которая заведёт удаление «для порядка», — и обнаружится она через
годы, когда предъявлять станет нечего.

Тест поэтому сторожевой, того же вида, что проверка расписания Celery
и путей контракта: он краснеет, когда в коде появляется удаление архивной
строки, и заставляет объяснить его вслух, а не поставить молча.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from aerogram.documents.service import RETENTION_YEARS

#: Таблицы архива: по ним ведётся отчётность и разбираются споры
#: с перевозчиками. Модель, которую нельзя удалять, названа именем класса —
#: переименование модели должно ломать тест, а не тихо снимать охрану.
ARCHIVE_MODELS = frozenset(
    {
        "Shipment",
        "ShipmentItem",
        "Decision",
        "Recommendation",
        "RateQuote",
        "RateOffer",
        "CostComponent",
        "Document",
        "DeliveryOutcome",
    }
)

#: Всё удаление, какое есть в коде, — по имени так, как оно написано.
#:
#: Список закрытый, и в этом весь смысл: новое удаление ЛЮБОЙ строки
#: краснит тест и требует объяснить его здесь вслух. Так проверка ловит
#: и то, о чём этот файл не догадывается, — например, удаление модели,
#: которую заведут завтра.
#:
#: ``CarrierRawCall`` — сырьё вызовов, тридцать суток (раздел 8.2 ТЗ):
#: это обязанность УДАЛИТЬ, а не сохранить, и она не про накладные.
#: ``rule`` — правило маршрутизации: корпоративная политика, а не документ.
#: Удалённое правило не переписывает принятые решения — они несут версию
#: прежней политики.
ALLOWED_DELETES = frozenset({"CarrierRawCall", "rule"})


def _is_session(node: ast.expr) -> bool:
    """Похоже ли выражение на сессию SQLAlchemy."""
    if isinstance(node, ast.Name):
        return "session" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "session" in node.attr.lower()
    return False


def _delete_targets(source: Path) -> set[str]:
    """Имена, у которых в этом файле что-то удаляют.

    Разбор синтаксисом, а не поиском подстроки: ``delete`` встречается
    и в маршрутах HTTP (``@router.delete``), и в клиенте хранилища,
    и подстрочный поиск краснел бы на них, а на настоящем удалении —
    молчал бы среди шума.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        # ``session.delete(obj)`` — удаление объекта ORM. Получатель обязан
        # быть сессией: иначе под проверку попадёт любой метод с именем
        # ``delete`` — например, ``RoutingRuleService(...).delete(rule_id)``,
        # который сам по себе ничего не удаляет, а зовёт репозиторий.
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "delete"
            and _is_session(target.value)
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            found.add(node.args[0].id)
        # ``delete(Model)`` — удаление запросом.
        if isinstance(target, ast.Name) and target.id == "delete" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name):
                found.add(first.id)
    return found


def _all_deletes(sources: list[Path]) -> dict[str, list[str]]:
    """Все удаления в коде: имя → файлы, где оно встретилось."""
    found: dict[str, list[str]] = {}
    for source in sources:
        for name in _delete_targets(source):
            found.setdefault(name, []).append(source.name)
    return found


class TestNothingDeletesTheArchive:
    def test_every_delete_is_declared(self, source_files: list[Path]) -> None:
        """Новое удаление любой строки требует объяснить себя вслух.

        Проверяется всё удаление, а не только по списку архивных моделей:
        имя переменной может быть каким угодно, а таблица — заведённой
        завтра. Закрытый список ловит и то, о чём этот файл не догадывается.
        """
        undeclared = {
            name: files
            for name, files in _all_deletes(source_files).items()
            if name not in ALLOWED_DELETES
        }
        assert not undeclared, (
            "удаление не объявлено: срок хранения архива не наш, и снимать "
            f"его нужно решением человека — {sorted(undeclared.items())}"
        )

    def test_no_archive_model_is_deleted(self, source_files: list[Path]) -> None:
        """Второй, узкий невод: удаление архивной модели по имени.

        Проверка без учёта регистра ловит обе записи разом — и
        ``delete(Shipment)``, и ``session.delete(shipment)``.
        """
        archive = {name.lower() for name in ARCHIVE_MODELS}
        offenders = sorted(
            f"{files[0]}: {name}"
            for name, files in _all_deletes(source_files).items()
            if name.lower() in archive
        )
        assert not offenders, f"удаление архивной строки: {offenders}"

    def test_the_allowed_list_stays_honest(self, source_files: list[Path]) -> None:
        """Разрешение, которым никто не пользуется, — забытое разрешение."""
        stale = sorted(ALLOWED_DELETES - set(_all_deletes(source_files)))
        assert not stale, f"больше не удаляется, убрать из списка: {stale}"


#: Опубликованная политика кабинета. Читается отсюда намеренно: срок
#: хранения назван и в ней, и в коде, и разойтись они не имеют права —
#: политика тогда соврёт, а заметят это через годы.
CABINET_LEGAL = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "legal.ts"


class TestTheTermIsNamedOnce:
    def test_five_years_at_least(self) -> None:
        """Решение человека: не менее пяти лет (ADR-0030)."""
        assert RETENTION_YEARS >= 5

    def test_the_cabinet_policy_names_the_same_term(self) -> None:
        """Число в политике и число в коде — одно и то же число.

        Тест читает файл фронта из бэкенда, и это не небрежность:
        расхождение здесь — не рассинхрон двух копий, а обещание клиенту,
        которого код не исполняет. Ловить его нужно там, где считают срок.
        """
        if not CABINET_LEGAL.exists():  # pragma: no cover — бэкенд без фронта
            pytest.skip("политика кабинета не найдена рядом с бэкендом")

        published = re.search(
            r"export const RETENTION_YEARS = (\d+)", CABINET_LEGAL.read_text(encoding="utf-8")
        )
        assert published, "в политике кабинета нет срока хранения"
        assert int(published.group(1)) == RETENTION_YEARS
