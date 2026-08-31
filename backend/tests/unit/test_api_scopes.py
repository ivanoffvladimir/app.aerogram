"""Таблица областей API-ключа классифицирует каждый путь с субъектом.

Смысл проверки. Умолчание в ``core.scopes`` — запрет, поэтому забытый путь
не откроется машинному клиенту случайно. Но обратная ошибка так же дорога:
путь, который обязан быть доступен интеграции, молча окажется закрытым,
и узнает об этом клиент, а не мы. Тест требует решения по каждому пути:
либо у него есть область, либо он назван кабинетным.

Заодно ловится третий случай — путь, исчезнувший из приложения, но оставшийся
в таблице. Такая строка выглядит работающим правилом и не значит ничего.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from aerogram.core.deps import current_principal
from aerogram.core.scopes import (
    ALL_SCOPES,
    CABINET_ONLY,
    MACHINE_SCOPES,
    SCOPE_LABELS,
    WITHOUT_SCOPE,
)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Приложение целиком. К базе не подключается: нужны только маршруты."""
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://aerogram_app:app@127.0.0.1:5433/aerogram"
    )
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
    os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-validation")
    os.environ.setdefault("CREDENTIAL_KEYS", "k1:" + "A" * 43 + "=")

    from aerogram.config import get_settings
    from aerogram.main import create_app

    get_settings.cache_clear()
    return create_app()


#: Пути без субъекта: до проверки области они не доходят и доходить не должны.
#: Перечислены поимённо, потому что «путь без аутентификации» — это решение,
#: а не следствие того, что зависимость забыли приписать.
PUBLIC: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/health/ready"),
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/refresh"),
        # Вебхук перевозчика: тенант неизвестен, пока отправление не найдено
        # по номеру заказа, подпись проверяется внутри (ADR-0015).
        ("POST", "/v1/webhooks/{carrier_code}"),
    }
)


def _routes(app: FastAPI, prefix: str = "") -> list[tuple[str, APIRoute]]:
    """Все маршруты приложения с их полными путями.

    Вложенные роутеры в свежих версиях FastAPI хранятся отдельными объектами
    и префикс держат рядом, а не в самом маршруте, — поэтому путь собирается
    здесь. Правильность сборки проверяется отдельным тестом: обход, который
    молча ничего не нашёл, сделал бы зелёными все проверки этого файла.
    """
    found: list[tuple[str, APIRoute]] = []
    for route in getattr(app, "routes", []):
        if isinstance(route, APIRoute):
            found.append((prefix + route.path_format, route))
            continue
        nested = getattr(route, "original_router", None)
        if nested is not None:
            context = getattr(route, "include_context", None)
            found.extend(_routes(nested, prefix + getattr(context, "prefix", "")))
    return found


def _all_keys(app: FastAPI) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for path, route in _routes(app):
        for method in route.methods:
            if method not in {"HEAD", "OPTIONS"}:
                keys.add((method, path))
    return keys


def _with_principal(app: FastAPI) -> set[tuple[str, str]]:
    """Пути, у которых есть субъект: только они доходят до проверки области."""
    keys: set[tuple[str, str]] = set()
    for path, route in _routes(app):
        if not _needs_principal(route):
            continue
        for method in route.methods:
            if method not in {"HEAD", "OPTIONS"}:
                keys.add((method, path))
    return keys


def _needs_principal(route: APIRoute) -> bool:
    """Требует ли маршрут субъекта — прямо или через ``require_roles``."""
    stack: list[Any] = [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is current_principal:
            return True
        stack.extend(dependant.dependencies)
    return False


class TestTheWalkItself:
    """Обход маршрутов обязан находить их все.

    Без этой проверки поломка обхода делала бы зелёным весь файл: пустое
    множество проходит любую проверку «ничего не забыто».
    """

    def test_every_documented_path_is_found(self, app: FastAPI) -> None:
        documented = {
            (method.upper(), path)
            for path, operations in app.openapi()["paths"].items()
            for method in operations
        }

        assert _all_keys(app) == documented

    def test_paths_without_a_principal_are_exactly_the_public_ones(self, app: FastAPI) -> None:
        """Новый путь без аутентификации обязан быть замечен здесь."""
        assert _all_keys(app) - _with_principal(app) == PUBLIC


class TestEveryPathIsClassified:
    def test_no_path_is_left_undecided(self, app: FastAPI) -> None:
        undecided = sorted(
            key
            for key in _with_principal(app)
            if key not in MACHINE_SCOPES and key not in CABINET_ONLY and key not in WITHOUT_SCOPE
        )

        assert not undecided, (
            "эти пути не классифицированы в core.scopes — они закрыты "
            f"для машинного клиента по умолчанию, и это надо подтвердить: {undecided}"
        )

    def test_the_table_names_no_vanished_path(self, app: FastAPI) -> None:
        """Строка о несуществующем пути выглядит правилом и не значит ничего."""
        live = _with_principal(app)
        stale = sorted(
            key for key in (set(MACHINE_SCOPES) | CABINET_ONLY | WITHOUT_SCOPE) if key not in live
        )

        assert not stale, f"в таблице областей пути, которых нет в приложении: {stale}"

    def test_a_path_is_not_both_open_and_cabinet_only(self) -> None:
        assert not set(MACHINE_SCOPES) & CABINET_ONLY
        assert not set(WITHOUT_SCOPE) & CABINET_ONLY


class TestVocabulary:
    def test_every_used_scope_can_be_issued(self) -> None:
        """Область, которой нет в словаре, невозможно выдать — путь был бы мёртв."""
        assert set(MACHINE_SCOPES.values()) <= set(ALL_SCOPES)

    def test_every_issuable_scope_opens_something(self) -> None:
        """Обратное: выдаваемая область, ничего не открывающая, вводит в заблуждение."""
        assert set(ALL_SCOPES) <= set(MACHINE_SCOPES.values())

    def test_every_scope_has_a_russian_label(self) -> None:
        assert set(SCOPE_LABELS) == set(ALL_SCOPES)


class TestSensitivePathsStayClosed:
    @pytest.mark.parametrize(
        "key",
        [
            ("POST", "/v1/api-keys"),
            ("GET", "/v1/api-keys"),
            ("DELETE", "/v1/api-keys/{key_id}"),
            ("POST", "/v1/users"),
            ("POST", "/v1/auth/mfa/disable"),
        ],
    )
    def test_a_stolen_key_cannot_widen_itself(self, key: tuple[str, str]) -> None:
        """Иначе украденный ключ превращается в постоянный доступ.

        Выпустить себе второй ключ, завести пользователя или снять второй
        фактор — три способа пережить отзыв первого ключа.
        """
        assert key in CABINET_ONLY
        assert key not in MACHINE_SCOPES
