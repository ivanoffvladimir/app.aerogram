"""Маскирование персональных данных в логах (12.7 ТЗ).

Полные значения ФИО, телефонов и адресов — только в БД. В логи они попадать не должны:
логи уезжают в GlitchTip и в файлы, у которых другой контур доступа.
"""

from __future__ import annotations

from aerogram.shared.logging import mask_pd, mask_secret


class TestMaskPd:
    def test_phone_keeps_only_tail(self) -> None:
        masked = mask_pd("+79161234567")
        assert masked == "***4567"
        assert "7916123" not in masked

    def test_email_keeps_domain_for_diagnostics(self) -> None:
        # Домен нужен, чтобы понять, о каком контуре речь; локальная часть — нет.
        assert mask_pd("ivan.petrov@example.ru") == "i***@example.ru"

    def test_short_value_is_fully_hidden(self) -> None:
        assert mask_pd("Иван") == "***"

    def test_empty_value_stays_empty(self) -> None:
        assert mask_pd("") == ""

    def test_full_name_is_not_recoverable(self) -> None:
        masked = mask_pd("Петров Иван Сергеевич")
        assert "Петров" not in masked
        assert "Иван" not in masked


class TestMaskSecret:
    def test_secret_is_fully_hidden(self) -> None:
        masked = mask_secret("super-secret-client-secret")
        assert "secret" not in masked.replace("<скрыто:", "")
        assert masked.startswith("<скрыто:")

    def test_length_is_preserved_for_debugging(self) -> None:
        # Длина помогает отличить «пустой ключ» от «неверный ключ» без раскрытия.
        assert mask_secret("abcdef") == "<скрыто:6>"
