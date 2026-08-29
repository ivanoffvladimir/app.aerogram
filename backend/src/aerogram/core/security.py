"""Пароли, токены и API-ключи.

12.3 ТЗ: пароли — Argon2id. Токены — JWT HS256. API-ключ показывается пользователю
один раз при выпуске; в БД лежит только хеш, восстановить ключ невозможно.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from aerogram.config import Settings
from aerogram.shared.clock import utcnow
from aerogram.shared.errors import AuthenticationError

__all__ = [
    "API_KEY_PREFIX",
    "TokenPayload",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "generate_api_key",
    "hash_api_key",
    "hash_password",
    "needs_rehash",
    "verify_password",
]

# Параметры Argon2id. Подобраны под VPS 4 vCPU / 8 ГБ: ~50 мс на проверку —
# достаточно дорого для перебора и незаметно для входа.
_hasher = PasswordHasher(
    time_cost=3, memory_cost=64 * 1024, parallelism=4, hash_len=32, salt_len=16
)

API_KEY_PREFIX = "ak"
_API_KEY_BYTES = 32


def hash_password(password: str) -> str:
    """Захешировать пароль Argon2id."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Проверить пароль. Не поднимает исключение при несовпадении."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Нужно ли перехешировать пароль после смены параметров Argon2."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Разобранное содержимое JWT."""

    user_id: UUID
    tenant_id: UUID
    role: str
    token_type: Literal["access", "refresh"]
    jti: str


def _create_token(
    *,
    settings: Settings,
    user_id: UUID,
    tenant_id: UUID,
    role: str,
    token_type: Literal["access", "refresh"],
    ttl: timedelta,
) -> str:
    now = utcnow()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "typ": token_type,
        "jti": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(settings: Settings, user_id: UUID, tenant_id: UUID, role: str) -> str:
    """Токен доступа, короткоживущий."""
    return _create_token(
        settings=settings,
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        token_type="access",  # noqa: S106  # тип токена, не пароль
        ttl=timedelta(minutes=settings.access_token_ttl_minutes),
    )


def create_refresh_token(settings: Settings, user_id: UUID, tenant_id: UUID, role: str) -> str:
    """Токен обновления."""
    return _create_token(
        settings=settings,
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        token_type="refresh",  # noqa: S106  # тип токена, не пароль
        ttl=timedelta(days=settings.refresh_token_ttl_days),
    )


def decode_token(settings: Settings, token: str) -> TokenPayload:
    """Разобрать и проверить JWT. Любая проблема — 401, без подробностей наружу."""
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Срок действия токена истёк") from None
    except jwt.PyJWTError:
        raise AuthenticationError("Некорректный токен") from None

    token_type = data.get("typ")
    if token_type not in ("access", "refresh"):
        raise AuthenticationError("Некорректный токен")

    try:
        return TokenPayload(
            user_id=UUID(data["sub"]),
            tenant_id=UUID(data["tid"]),
            role=str(data["role"]),
            token_type=token_type,
            jti=str(data.get("jti", "")),
        )
    except (KeyError, ValueError):
        raise AuthenticationError("Некорректный токен") from None


def generate_api_key(environment: str) -> tuple[str, str, str]:
    """Выпустить API-ключ.

    Возвращает тройку «полный ключ, префикс для отображения, хеш для хранения».
    Полный ключ показывается пользователю один раз и больше не восстановим (FR-10.2).
    """
    scope = "live" if environment == "production" else "test"
    secret = secrets.token_urlsafe(_API_KEY_BYTES)
    full = f"{API_KEY_PREFIX}_{scope}_{secret}"
    return full, full[:16], hash_api_key(full)


def hash_api_key(key: str) -> str:
    """Хеш API-ключа.

    SHA-256, а не Argon2: ключ проверяется на каждом запросе API и обладает полной
    энтропией 256 бит — медленный хеш здесь не добавляет стойкости, а добавляет
    задержку к p95 (раздел 11 ТЗ).
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """Сравнение без утечки по времени."""
    return hmac.compare_digest(left, right)
