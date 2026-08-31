"""Сквозные проверки API: вход, профиль, изоляция на уровне HTTP.

Критерий приёмки 14.2, п. 11 требует, чтобы обращение к чужому объекту давало 404.
Проверять это только на уровне БД мало: до БД запрос доходит через слой прав,
и ошибиться можно там.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pyotp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.core.models import ApiKey, Tenant, User
from aerogram.core.security import generate_api_key, hash_password
from aerogram.db import set_tenant
from aerogram.shared import totp
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


async def _issue_api_key(database_url: str, tenant_id: UUID) -> str:
    """Выдать ключ машинного клиента и вернуть его в открытом виде."""
    engine = create_async_engine(os.getenv("TEST_MIGRATION_DATABASE_URL", database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    raw, prefix, key_hash = generate_api_key("local")
    async with factory() as db, db.begin():
        await set_tenant(db, tenant_id)
        db.add(
            ApiKey(
                tenant_id=tenant_id,
                name="Машинный клиент",
                key_hash=key_hash,
                prefix=prefix,
                scopes=["rates:read"],
            )
        )
    await engine.dispose()
    return raw


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


class Clock:
    """Управляемое время для проверок второго фактора.

    Настоящие часы сделали бы тесты мигающими на границе тридцатисекундного
    шага и не дали бы проверить главное — что один шаг принимается один раз.
    """

    def __init__(self) -> None:
        self.moment = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def advance(self, seconds: int) -> None:
        self.moment += timedelta(seconds=seconds)

    def code(self, secret: str, *, shift: int = 0) -> str:
        return str(pyotp.TOTP(secret).at(self.moment + timedelta(seconds=shift)))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Подменить часы в проверке второго фактора."""
    controlled = Clock()
    monkeypatch.setattr("aerogram.core.service.utcnow", lambda: controlled.moment)
    return controlled


class TestSecondFactor:
    """Второй фактор: подключение, вход по коду и отказ принять код дважды."""

    async def _authorize(self, app: FastAPI, email: str = "a@example.com") -> str:
        """Токен на пароле — состояние до подключения фактора."""
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/login", json={"email": email, "password": PASSWORD}
            )
        response.raise_for_status()
        token: str = response.json()["access_token"]
        return token

    async def _setup(self, app: FastAPI) -> str:
        """Пройти подключение через API и вернуть выданный секрет."""
        token = await self._authorize(app)
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/mfa/setup", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200, response.text
        secret: str = response.json()["secret"]
        return secret

    async def _enable(self, app: FastAPI, clock: Clock, secret: str) -> None:
        token = await self._authorize(app)
        async with await _client(app) as client:
            response = await client.post(
                "/v1/auth/mfa/enable",
                json={"code": clock.code(secret)},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["mfa_enabled"] is True
        # Подтверждение сожгло свой шаг: следующий код будет уже другим.
        clock.advance(totp.INTERVAL_SECONDS)

    async def _connected(self, app: FastAPI, clock: Clock) -> str:
        """Пользователь с включённым вторым фактором. Возвращает секрет."""
        secret = await self._setup(app)
        await self._enable(app, clock, secret)
        return secret

    async def _login(self, app: FastAPI, code: str | None) -> Response:
        body: dict[str, object] = {"email": "a@example.com", "password": PASSWORD}
        if code is not None:
            body["mfa_code"] = code
        async with await _client(app) as client:
            return await client.post("/v1/auth/login", json=body)

    async def _token_with_code(self, app: FastAPI, clock: Clock, secret: str) -> str:
        response = await self._login(app, clock.code(secret))
        response.raise_for_status()
        clock.advance(totp.INTERVAL_SECONDS)
        token: str = response.json()["access_token"]
        return token

    async def test_setup_returns_secret_and_otpauth_url(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
        token = await self._authorize(app)
        async with await _client(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            response = await client.post("/v1/auth/mfa/setup", headers=headers)
            me = await client.get("/v1/auth/me", headers=headers)

        body = response.json()
        assert response.status_code == 200
        # Ссылка существует ровно затем, чтобы показать её QR-кодом.
        assert body["otpauth_url"].startswith("otpauth://totp/")
        assert body["secret"] in body["otpauth_url"]
        # Пока код не подтверждён, фактор не включён: неверно снятый QR-код
        # иначе запирал бы вход навсегда.
        assert me.json()["mfa_enabled"] is False

    async def test_secret_is_shown_once_and_never_again(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        """Украденная сессия не должна давать постоянный обход фактора."""
        secret = await self._connected(app, clock)
        token = await self._token_with_code(app, clock, secret)

        async with await _client(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            me = await client.get("/v1/auth/me", headers=headers)
            again = await client.post("/v1/auth/mfa/setup", headers=headers)

        assert secret not in me.text
        assert again.status_code == 409

    async def test_login_with_valid_code(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        secret = await self._connected(app, clock)

        response = await self._login(app, clock.code(secret))

        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

    async def test_login_without_code_is_refused(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        """Правильного пароля мало — ровно в этом смысл второго фактора."""
        await self._connected(app, clock)

        response = await self._login(app, None)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    async def test_login_with_wrong_code_is_refused(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        await self._connected(app, clock)
        stranger = clock.code(totp.generate_secret())

        response = await self._login(app, stranger)

        assert response.status_code == 401

    async def test_code_from_a_distant_step_is_refused(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        """Окно ±1 шаг: код десятиминутной давности не принимается."""
        secret = await self._connected(app, clock)

        response = await self._login(app, clock.code(secret, shift=-600))

        assert response.status_code == 401

    async def test_the_same_code_does_not_work_twice(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        """Подсмотренный код годен до конца своих тридцати секунд — но один раз."""
        secret = await self._connected(app, clock)
        code = clock.code(secret)

        first = await self._login(app, code)
        second = await self._login(app, code)

        assert first.status_code == 200, first.text
        assert second.status_code == 401
        assert second.json()["error"]["code"] == "unauthenticated"

    async def test_a_code_older_than_the_burnt_step_is_refused(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        """Соседний шаг попадает в окно, но назад отсечка не пускает.

        Иначе перехваченный код предыдущего шага оставался бы годным
        после того, как вход по свежему коду уже состоялся.
        """
        secret = await self._connected(app, clock)
        previous = clock.code(secret, shift=-totp.INTERVAL_SECONDS)
        assert (await self._login(app, clock.code(secret))).status_code == 200

        response = await self._login(app, previous)

        assert response.status_code == 401

    async def test_disable_needs_a_code_and_reopens_login(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], clock: Clock
    ) -> None:
        secret = await self._connected(app, clock)
        token = await self._token_with_code(app, clock, secret)
        headers = {"Authorization": f"Bearer {token}"}

        async with await _client(app) as client:
            without = await client.post(
                "/v1/auth/mfa/disable", json={"code": "000000"}, headers=headers
            )
            done = await client.post(
                "/v1/auth/mfa/disable", json={"code": clock.code(secret)}, headers=headers
            )

        assert without.status_code == 401
        assert done.status_code == 200
        assert done.json()["mfa_enabled"] is False
        assert (await self._login(app, None)).status_code == 200

    async def test_api_key_client_has_no_second_factor(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID], database_url: str
    ) -> None:
        """Машинному клиенту фактор подключать некому: ключ отзывается."""
        tenant_a, _, _ = seeded
        raw = await _issue_api_key(database_url, tenant_a)

        async with await _client(app) as client:
            response = await client.post("/v1/auth/mfa/setup", headers={"X-Api-Key": raw})

        assert response.status_code == 403

    async def test_users_without_mfa_still_log_in(
        self, app: FastAPI, seeded: tuple[UUID, UUID, UUID]
    ) -> None:
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
