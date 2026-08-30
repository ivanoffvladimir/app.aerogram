"""Сквозные проверки API: вход, профиль, изоляция на уровне HTTP.

Критерий приёмки 14.2, п. 11 требует, чтобы обращение к чужому объекту давало 404.
Проверять это только на уровне БД мало: до БД запрос доходит через слой прав,
и ошибиться можно там.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.core.models import Tenant, User
from aerogram.core.security import hash_password
from aerogram.db import set_tenant
from aerogram.shared.enums import TenantStatus, UserRole
from aerogram.shared.ids import uuid7

pytestmark = pytest.mark.integration

PASSWORD = "test-password-12345"


@pytest.fixture
async def clean_db(database_url: str) -> AsyncIterator[None]:
    """Очистить бизнес-таблицы перед тестом.

    Тесты API работают с настоящими транзакциями, как в проде: подменять их
    savepoint-ами значит проверять не тот код, который поедет на сервер.
    Поэтому изоляция достигается очисткой, а не откатом.
    """
    url = os.getenv("TEST_MIGRATION_DATABASE_URL", database_url)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text("TRUNCATE tenants, users, counterparties, addresses CASCADE"))
    await engine.dispose()
    yield


@pytest.fixture
async def app(database_url: str, clean_db: None) -> AsyncIterator[FastAPI]:
    """Приложение, работающее с БД ровно так же, как в проде."""
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
    os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-validation")
    os.environ.setdefault("CREDENTIAL_KEYS", "k1:" + "A" * 43 + "=")

    from aerogram import db as db_module
    from aerogram.config import get_settings
    from aerogram.main import create_app

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None

    application = create_app()
    try:
        yield application
    finally:
        engine = db_module._engine
        if engine is not None:
            await engine.dispose()
        db_module._engine = None
        db_module._sessionmaker = None
        get_settings.cache_clear()


@pytest.fixture
async def seeded(app: FastAPI, database_url: str) -> tuple[UUID, UUID, UUID]:
    """Два тенанта и пользователь в каждом. Возвращает ``(tenant_a, user_a, user_b)``."""
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid7(), uuid7()
    users: dict[UUID, UUID] = {}

    async with factory() as db, db.begin():
        db.add_all(
            [
                Tenant(id=tenant_a, name="Роспломба", status=TenantStatus.ACTIVE, plan="pilot"),
                Tenant(id=tenant_b, name="Конкурент", status=TenantStatus.ACTIVE, plan="pilot"),
            ]
        )
        await db.flush()
        for tenant_id, email in ((tenant_a, "a@example.com"), (tenant_b, "b@example.com")):
            await set_tenant(db, tenant_id)
            user = User(
                tenant_id=tenant_id,
                email=email,
                full_name="Логист Тестовый",
                role=UserRole.LOGISTICIAN,
                password_hash=hash_password(PASSWORD),
                is_active=True,
            )
            db.add(user)
            await db.flush()
            users[tenant_id] = user.id
    await engine.dispose()
    return tenant_a, users[tenant_a], users[tenant_b]


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestHealth:
    async def test_health_needs_no_auth(self, app: FastAPI) -> None:
        async with await _client(app) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_response_carries_request_id(self, app: FastAPI) -> None:
        # Сквозной request_id — требование наблюдаемости раздела 11 ТЗ.
        async with await _client(app) as client:
            response = await client.get("/health")
        assert response.headers["X-Request-Id"]


class TestLogin:
    async def test_valid_credentials_return_token_pair(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["token_type"] == "bearer"

    async def test_wrong_password_is_401_without_details(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": "a@example.com", "password": "неверный"}
            )
        assert response.status_code == 401
        # Один и тот же текст для неверного e-mail и неверного пароля: иначе
        # получается оракул для перебора учётных записей.
        assert response.json()["error"]["message"] == "Неверный e-mail или пароль"

    async def test_unknown_email_gives_same_message(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": "нет@example.com", "password": PASSWORD}
            )
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Неверный e-mail или пароль"

    async def test_error_body_matches_specified_format(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": "a@example.com", "password": "неверный"}
            )
        assert set(response.json()["error"]) == {
            "code",
            "message",
            "field",
            "carrier_code",
            "request_id",
        }

    async def test_malformed_body_reports_field(self, app: FastAPI) -> None:
        async with await _client(app) as client:
            response = await client.post("/v1/auth/login", json={"email": "не-почта"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"
        assert response.json()["error"]["field"] is not None


class TestAuthorizedAccess:
    async def test_me_returns_own_profile(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        async with await _client(app) as client:
            token = (
                await client.post(
                    "/v1/auth/login",
                    json={"email": "a@example.com", "password": PASSWORD},
                )
            ).json()["access_token"]
            response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["email"] == "a@example.com"

    async def test_without_token_is_401(self, app: FastAPI) -> None:
        async with await _client(app) as client:
            response = await client.get("/v1/auth/me")
        assert response.status_code == 401

    async def test_refresh_token_is_not_accepted_as_access(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        async with await _client(app) as client:
            tokens = (
                await client.post(
                    "/v1/auth/login",
                    json={"email": "a@example.com", "password": PASSWORD},
                )
            ).json()
            response = await client.get(
                "/v1/auth/me",
                headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
            )
        assert response.status_code == 401

    async def test_user_list_shows_only_own_tenant(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        async with await _client(app) as client:
            token = (
                await client.post(
                    "/v1/auth/login",
                    json={"email": "a@example.com", "password": PASSWORD},
                )
            ).json()["access_token"]
            response = await client.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
        emails = {u["email"] for u in response.json()}
        assert "a@example.com" in emails
        assert "b@example.com" not in emails
