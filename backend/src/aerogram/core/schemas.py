"""DTO ядра. Контракт между слоями и с фронтом (генерируется в клиент из OpenAPI)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from aerogram.shared.enums import TenantStatus, UserRole

__all__ = [
    "ErrorBody",
    "ErrorResponse",
    "LoginRequest",
    "RefreshRequest",
    "TenantOut",
    "TokenPair",
    "UserCreate",
    "UserOut",
]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    #: Код TOTP. Обязателен для роли owner (12.5 ТЗ).
    mfa_code: str | None = Field(default=None, min_length=6, max_length=6)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105  # схема авторизации, не пароль
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    mfa_enabled: bool
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role: UserRole
    password: str = Field(min_length=12, max_length=128)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    inn: str | None = None
    status: TenantStatus
    plan: str
    timezone: str


class ErrorBody(BaseModel):
    """Единый формат ошибки (FR-10.5)."""

    code: str
    message: str
    field: str | None = None
    carrier_code: str | None = None
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
