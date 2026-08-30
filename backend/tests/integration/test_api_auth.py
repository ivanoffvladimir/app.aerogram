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


class TestSecondFactor:
    """Второй фактор: пока его нечем проверить, вход закрыт наглухо."""

    async def _enable_mfa(self, database_url: str, tenant_id: UUID, user_id: UUID) -> None:
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            await conn.execute(
                text("UPDATE users SET mfa_enabled = true WHERE id = :i"), {"i": user_id}
            )
            await conn.commit()
        await engine.dispose()

    async def test_any_six_characters_no_longer_pass_as_a_code(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], database_url: str
    ) -> None:
        """Прежняя проверка принимала любую строку из шести символов.

        Это не давало защиты именно в том случае, ради которого второй фактор
        и существует: когда пароль уже украден.
        """
        tenant_a, user_a, _ = seeded
        await self._enable_mfa(database_url, tenant_a, user_a)

        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login",
                json={"email": "a@example.com", "password": PASSWORD, "mfa_code": "000000"},
            )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    async def test_correct_password_alone_is_not_enough_either(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], database_url: str
    ) -> None:
        """Падаем закрыто: без кода тоже отказ, а не пропуск."""
        tenant_a, user_a, _ = seeded
        await self._enable_mfa(database_url, tenant_a, user_a)

        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD}
            )
        assert response.status_code == 401

    async def test_users_without_mfa_still_log_in(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        """Заглушка не должна закрыть вход всем подряд."""
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD}
            )
        assert response.status_code == 200


class TestRoleAssignment:
    """Владелец тенанта не может выдать платформенную роль."""

    async def _owner_token(self, app: FastAPI, database_url: str, tenant_id: UUID) -> str:
        engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            await conn.execute(
                text("UPDATE users SET role = 'owner' WHERE email = 'a@example.com'")
            )
            await conn.commit()
        await engine.dispose()

        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD}
            )
        token: str = response.json()["access_token"]
        return token

    async def test_platform_admin_is_not_assignable_through_the_api(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], database_url: str
    ) -> None:
        """Иначе клиент выдаёт себе доступ к общим справочникам всех тенантов.

        `city_carrier_map` не имеет tenant_id и читается на расчёте у каждого
        тенанта: испортив её, один клиент ломает оформление всем остальным.
        """
        tenant_a, _, _ = seeded
        token = await self._owner_token(app, database_url, tenant_a)

        async with await _client(app) as client:
            response = await client.post(
                "/v1/users",
                json={
                    "email": "attacker@example.com",
                    "full_name": "Чужой",
                    "role": "platform_admin",
                    "password": "long-enough-password",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 422, response.text
        assert "role" in (response.json()["error"]["field"] or "")

    async def test_support_is_not_assignable_either(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], database_url: str
    ) -> None:
        tenant_a, _, _ = seeded
        token = await self._owner_token(app, database_url, tenant_a)

        async with await _client(app) as client:
            response = await client.post(
                "/v1/users",
                json={
                    "email": "support@example.com",
                    "full_name": "Поддержка",
                    "role": "support",
                    "password": "long-enough-password",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 422

    async def test_tenant_roles_are_still_assignable(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], database_url: str
    ) -> None:
        """Запрет не должен закрыть обычное заведение сотрудников."""
        tenant_a, _, _ = seeded
        token = await self._owner_token(app, database_url, tenant_a)

        async with await _client(app) as client:
            response = await client.post(
                "/v1/users",
                json={
                    "email": "operator@example.com",
                    "full_name": "Оператор",
                    "role": "operator",
                    "password": "long-enough-password",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 201, response.text
        assert response.json()["role"] == "operator"
