"""Шифрование учётных данных перевозчиков (12.3 ТЗ)."""

from __future__ import annotations

import pytest

from aerogram.shared.crypto import CredentialCipher, EncryptedBlob


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher({"k1": CredentialCipher.generate_key()}, active_key_id="k1")


class TestRoundTrip:
    def test_decrypts_what_it_encrypted(self, cipher: CredentialCipher) -> None:
        secret = '{"client_id": "abc", "client_secret": "s3cr3t"}'
        assert cipher.decrypt(cipher.encrypt(secret)) == secret

    def test_ciphertext_differs_between_calls(self, cipher: CredentialCipher) -> None:
        # Одинаковый шифротекст выдал бы одинаковые учётные данные у разных тенантов.
        assert cipher.encrypt("одно и то же") != cipher.encrypt("одно и то же")

    def test_plaintext_is_not_present_in_envelope(self, cipher: CredentialCipher) -> None:
        assert "s3cr3t" not in cipher.encrypt("s3cr3t")


class TestAssociatedData:
    def test_aad_binds_ciphertext_to_owner(self, cipher: CredentialCipher) -> None:
        # Перенос шифротекста в чужую строку не должен расшифровываться.
        blob = cipher.encrypt("секрет", aad=b"account-1")
        assert cipher.decrypt(blob, aad=b"account-1") == "секрет"
        with pytest.raises(Exception):  # noqa: B017  # InvalidTag из cryptography
            cipher.decrypt(blob, aad=b"account-2")


class TestKeyRotation:
    def test_old_key_still_decrypts_after_rotation(self) -> None:
        old_key = CredentialCipher.generate_key()
        new_key = CredentialCipher.generate_key()

        old_cipher = CredentialCipher({"k1": old_key}, active_key_id="k1")
        blob = old_cipher.encrypt("секрет прошлого поколения")

        rotated = CredentialCipher({"k1": old_key, "k2": new_key}, active_key_id="k2")
        assert rotated.decrypt(blob) == "секрет прошлого поколения"
        # Новые записи шифруются уже актуальным ключом.
        assert EncryptedBlob.loads(rotated.encrypt("новое")).key_id == "k2"

    def test_missing_key_reports_clearly(self) -> None:
        blob = CredentialCipher({"k1": CredentialCipher.generate_key()}, "k1").encrypt("x")
        other = CredentialCipher({"k2": CredentialCipher.generate_key()}, "k2")
        with pytest.raises(ValueError, match="отозванным ключом"):
            other.decrypt(blob)


class TestConfiguration:
    def test_rejects_active_key_outside_key_set(self) -> None:
        with pytest.raises(ValueError, match="активный ключ"):
            CredentialCipher({"k1": CredentialCipher.generate_key()}, active_key_id="k9")

    def test_rejects_key_of_wrong_length(self) -> None:
        with pytest.raises(ValueError, match="ожидалось 32 байт"):
            CredentialCipher({"k1": "c2hvcnQ="}, active_key_id="k1")

    def test_rejects_unknown_envelope_version(self) -> None:
        with pytest.raises(ValueError, match="версия конверта"):
            EncryptedBlob.loads('{"v": 99, "key_id": "k1", "nonce": "", "ct": ""}')
