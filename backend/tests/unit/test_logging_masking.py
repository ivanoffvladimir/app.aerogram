"""Маскирование персональных данных в логах (12.7 ТЗ).

Полные значения ФИО, телефонов и адресов — только в БД. В логи они попадать не должны:
логи уезжают в GlitchTip и в файлы, у которых другой контур доступа.
"""

from __future__ import annotations

import pytest

from aerogram.shared.ids import uuid7
from aerogram.shared.logging import _mask_text, mask_pd, mask_secret


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


class TestMaskTextInFreeform:
    """Маска по свободному тексту: телефон закрыть, идентификатор не тронуть.

    Обе стороны одинаково важны, и раньше выполнялась только первая. Маска
    искала телефон где угодно, в том числе ВНУТРИ слова, — а идентификаторы
    состоят из тех же цифр и дефисов. Две соседние группы UUID, целиком
    цифровые, читаются как «8-999-123-45-67», и у каждого четвёртого
    документа середина ключа заменялась на ``***``.

    Ценой был не только мигающий тест. Строка ``documents.storage_failed``
    пишется ровно затем, чтобы по ней найти документ в базе; с замаскированной
    серединой она не находит ничего, а закрывает при этом не человека,
    а шестнадцатеричное число.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "+79161234567",
            "8-999-123-45-67",
            "Телефон 8 (999) 123-45-67 у получателя",
            "тел. +7 916 123-45-67.",
        ],
    )
    def test_phone_in_text_is_still_masked(self, text: str) -> None:
        masked = _mask_text(text)
        assert "***" in masked
        assert "1234567" not in masked.replace("-", "").replace(" ", "")

    def test_identifiers_survive_intact(self) -> None:
        # Тысяча настоящих ключей, а не один удачный: до починки не проходил
        # примерно каждый четвёртый, и поймать это одним примером нельзя.
        for _ in range(1000):
            key = f"{uuid7()}.pdf"
            assert _mask_text(key) == key

    def test_a_uuid_shaped_like_a_phone_survives(self) -> None:
        # Тот самый случай: две соседние группы целиком из цифр.
        key = "01a07f05-73f6-7805-9134-561278912634.pdf"
        assert _mask_text(key) == key
