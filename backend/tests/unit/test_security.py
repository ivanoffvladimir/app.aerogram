"""Пароли, токены и API-ключи (12.3, FR-10.2)."""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from aerogram.config import Settings
from aerogram.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.errors import AuthenticationError
from aerogram.shared.ids import uuid7


class TestPasswords:
    def test_verifies_correct_password(self) -> None:
        assert verify_password(hash_password("правильный-пароль-123"), "правильный-пароль-123")

    def test_rejects_wrong_password(self) -> None:
        assert not verify_password(hash_password("правильный-пароль-123"), "другой-пароль")

    def test_hash_is_argon2id(self) -> None:
        assert hash_password("пароль-для-проверки").startswith("$argon2id$")

    def test_same_password_gives_different_hashes(self) -> None:
        assert hash_password("пароль-для-проверки") != hash_password("пароль-для-проверки")

    def test_malformed_hash_does_not_raise(self) -> None:
        # Битая строка в БД не должна ронять вход с 500.
        assert verify_password("не-хеш-вовсе", "любой-пароль") is False


class TestTokens:
    def test_access_token_round_trip(self, settings: Settings) -> None:
        user_id, tenant_id = uuid7(), uuid7()
        payload = decode_token(
            settings, create_access_token(settings, user_id, tenant_id, "logistician")
        )
        assert payload.user_id == user_id
        assert payload.tenant_id == tenant_id
        assert payload.role == "logistician"
        assert payload.token_type == "access"

    def test_refresh_token_is_marked_as_such(self, settings: Settings) -> None:
        token = create_refresh_token(settings, uuid7(), uuid7(), "owner")
        assert decode_token(settings, token).token_type == "refresh"

    def test_expired_token_is_rejected(self, settings: Settings) -> None:
        expired = jwt.encode(
            {
                "sub": str(uuid7()),
                "tid": str(uuid7()),
                "role": "owner",
                "typ": "access",
                "exp": int((utcnow() - timedelta(minutes=1)).timestamp()),
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(AuthenticationError, match="истёк"):
            decode_token(settings, expired)

    def test_token_signed_by_another_key_is_rejected(self, settings: Settings) -> None:
        forged = jwt.encode({"sub": str(uuid7())}, "чужой-ключ", algorithm="HS256")
        with pytest.raises(AuthenticationError):
            decode_token(settings, forged)

    def test_token_without_required_claims_is_rejected(self, settings: Settings) -> None:
        broken = jwt.encode(
            {"typ": "access"}, settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
        with pytest.raises(AuthenticationError):
            decode_token(settings, broken)


class TestApiKeys:
    def test_issue_returns_key_prefix_and_hash(self) -> None:
        full, prefix, key_hash = generate_api_key("production")
        assert full.startswith("ak_live_")
        assert full.startswith(prefix)
        assert key_hash == hash_api_key(full)

    def test_non_production_keys_are_marked_test(self) -> None:
        full, _, _ = generate_api_key("staging")
        assert full.startswith("ak_test_")

    def test_hash_does_not_reveal_key(self) -> None:
        full, _, key_hash = generate_api_key("production")
        assert full not in key_hash

    def test_keys_are_unique(self) -> None:
        assert len({generate_api_key("production")[0] for _ in range(100)}) == 100
