"""Второй фактор: окно, отсечка повторов, устойчивость к мусору на входе.

Проверяется именно ``shared.totp``: если он молча начнёт принимать код
соседнего дня или перестанет возвращать номер шага, защита от повтора
превратится в пустую формальность, и заметить это на уровне HTTP тяжело.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from aerogram.shared import totp

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def secret() -> str:
    return totp.generate_secret()


class TestVerify:
    def test_current_code_matches_current_step(self, secret: str) -> None:
        reference = pyotp.TOTP(secret)

        step = totp.verify(secret, reference.at(NOW), at=NOW)

        assert step == reference.timecode(NOW)

    @pytest.mark.parametrize("shift", [-30, 30])
    def test_neighbouring_step_is_accepted(self, secret: str, shift: int) -> None:
        """±30 секунд — это расхождение часов телефона, а не атака."""
        reference = pyotp.TOTP(secret)
        moment = NOW + timedelta(seconds=shift)

        step = totp.verify(secret, reference.at(moment), at=NOW)

        assert step == reference.timecode(moment)

    @pytest.mark.parametrize("shift", [-120, 120, 3600])
    def test_distant_step_is_refused(self, secret: str, shift: int) -> None:
        code = pyotp.TOTP(secret).at(NOW + timedelta(seconds=shift))

        assert totp.verify(secret, code, at=NOW) is None

    def test_code_of_another_secret_is_refused(self, secret: str) -> None:
        stranger = pyotp.TOTP(totp.generate_secret()).at(NOW)

        assert totp.verify(secret, stranger, at=NOW) is None

    @pytest.mark.parametrize("garbage", ["", "abcdef", "12345", "12 34 56", "٠١٢٣٤٥"])
    def test_non_digits_are_refused_without_raising(self, secret: str, garbage: str) -> None:
        """Строку с фронта нельзя доверять: разбор не должен падать исключением."""
        assert totp.verify(secret, garbage, at=NOW) is None

    def test_returned_step_is_what_replay_protection_burns(self, secret: str) -> None:
        """Номер шага — единственное, чем отличить повтор от нового кода."""
        reference = pyotp.TOTP(secret)
        earlier = NOW - timedelta(seconds=30)

        assert totp.verify(secret, reference.at(earlier), at=NOW) == reference.timecode(earlier)
        assert totp.verify(secret, reference.at(NOW), at=NOW) == reference.timecode(NOW)

    def test_window_zero_accepts_only_the_current_step(self, secret: str) -> None:
        reference = pyotp.TOTP(secret)

        assert totp.verify(secret, reference.at(NOW), at=NOW, window=0) is not None
        neighbour = reference.at(NOW - timedelta(seconds=30))
        assert totp.verify(secret, neighbour, at=NOW, window=0) is None


class TestProvisioning:
    def test_uri_carries_secret_and_issuer(self, secret: str) -> None:
        uri = totp.provisioning_uri(secret, account_name="a@example.com", issuer="Aerogram")

        assert uri.startswith("otpauth://totp/")
        assert secret in uri
        assert "issuer=Aerogram" in uri

    def test_generated_secrets_differ(self) -> None:
        assert totp.generate_secret() != totp.generate_secret()
