"""Шифрование учётных данных перевозчиков (12.3 ТЗ): AES-256-GCM.

Ключ живёт в переменной окружения, не в БД и не в коде. Ротация — раз в год:
``key_id`` в конверте позволяет расшифровать старые записи ключом предыдущего
поколения, пока они не перешифрованы.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["CredentialCipher", "EncryptedBlob"]

_NONCE_BYTES = 12
_KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """Конверт шифротекста, пригодный для хранения в текстовой колонке."""

    key_id: str
    nonce: str
    ciphertext: str

    def dumps(self) -> str:
        return json.dumps(
            {"v": 1, "key_id": self.key_id, "nonce": self.nonce, "ct": self.ciphertext},
            separators=(",", ":"),
        )

    @classmethod
    def loads(cls, raw: str) -> EncryptedBlob:
        data = json.loads(raw)
        if data.get("v") != 1:
            raise ValueError(f"неизвестная версия конверта шифрования: {data.get('v')!r}")
        return cls(key_id=data["key_id"], nonce=data["nonce"], ciphertext=data["ct"])


class CredentialCipher:
    """AES-GCM поверх набора ключей.

    ``keys`` — отображение ``key_id -> ключ в base64``; ``active_key_id`` используется
    для новых записей. Старые ключи остаются для расшифровки до перешифрования.
    """

    def __init__(self, keys: dict[str, str], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise ValueError(f"активный ключ {active_key_id!r} отсутствует в наборе ключей")
        self._ciphers: dict[str, AESGCM] = {}
        for key_id, raw in keys.items():
            material = base64.b64decode(raw)
            if len(material) != _KEY_BYTES:
                raise ValueError(
                    f"ключ {key_id!r}: ожидалось {_KEY_BYTES} байт, получено {len(material)}"
                )
            self._ciphers[key_id] = AESGCM(material)
        self._active_key_id = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def encrypt(self, plaintext: str, *, aad: bytes | None = None) -> str:
        """Зашифровать строку. ``aad`` привязывает шифротекст к владельцу записи."""
        nonce = os.urandom(_NONCE_BYTES)
        cipher = self._ciphers[self._active_key_id]
        ct = cipher.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return EncryptedBlob(
            key_id=self._active_key_id,
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ct).decode("ascii"),
        ).dumps()

    def decrypt(self, raw: str, *, aad: bytes | None = None) -> str:
        """Расшифровать конверт. Ключ выбирается по ``key_id`` из самого конверта."""
        blob = EncryptedBlob.loads(raw)
        cipher = self._ciphers.get(blob.key_id)
        if cipher is None:
            raise ValueError(
                f"ключ {blob.key_id!r} недоступен: запись зашифрована отозванным ключом"
            )
        plaintext = cipher.decrypt(
            base64.b64decode(blob.nonce), base64.b64decode(blob.ciphertext), aad
        )
        return plaintext.decode("utf-8")

    @staticmethod
    def generate_key() -> str:
        """Сгенерировать новый ключ в base64 — для первичной настройки и ротации."""
        return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")
