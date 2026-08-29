"""Реестр адаптеров — единственная точка входа домена к перевозчикам."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aerogram.carriers import registry
from aerogram.carriers.base import Capabilities, CarrierAdapter
from aerogram.shared.enums import LabelFormat


class _FakeAdapter:
    """Минимальный адаптер для проверки реестра. Методы не реализованы намеренно."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.name = f"Перевозчик {code}"
        self.capabilities = Capabilities(
            supports_cancel=True,
            supported_label_formats=(LabelFormat.PDF_A6,),
        )


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


class TestRegistration:
    def test_registers_and_returns_adapter(self) -> None:
        adapter = _FakeAdapter("cdek")
        registry.register(adapter)
        assert registry.get_adapter("cdek") is adapter

    def test_duplicate_code_is_rejected(self) -> None:
        # Молчаливая перезапись адаптера означала бы, что заказы уходят не тому ТК.
        registry.register(_FakeAdapter("cdek"))
        with pytest.raises(ValueError, match="уже зарегистрирован"):
            registry.register(_FakeAdapter("cdek"))

    def test_unknown_code_reports_clearly(self) -> None:
        with pytest.raises(LookupError, match="не зарегистрирован"):
            registry.get_adapter("почта-россии")


class TestListing:
    def test_codes_are_sorted(self) -> None:
        for code in ("major", "cdek", "dellin"):
            registry.register(_FakeAdapter(code))
        assert registry.available_codes() == ("cdek", "dellin", "major")

    def test_empty_registry_is_not_an_error(self) -> None:
        assert registry.available_codes() == ()
        assert registry.all_adapters() == ()


class TestProtocolConformance:
    def test_capabilities_snapshot_is_json_serializable(self) -> None:
        # Снимок кладётся в carriers.capabilities (JSONB) и уходит в публичное API,
        # поэтому в нём не должно быть питоновских перечислений.
        snapshot = _FakeAdapter("cdek").capabilities.as_dict()
        assert snapshot["supported_label_formats"] == ["pdf_a6"]
        assert snapshot["supports_cancel"] is True

    def test_capabilities_defaults_are_conservative(self) -> None:
        """Умолчания говорят «не умею».

        Если адаптер забыл объявить возможность, платформа не должна пытаться ею
        воспользоваться — ошибка вылезет у клиента, а не в тесте адаптера.
        """
        capabilities = Capabilities()
        assert capabilities.supports_webhooks is False
        assert capabilities.supports_cancel is False
        assert capabilities.supported_label_formats == ()

    def test_runtime_checkable_protocol_accepts_complete_adapter(self) -> None:
        # Protocol проверяет наличие атрибутов; полный контракт держит mypy.
        assert isinstance(_FakeAdapter("cdek"), CarrierAdapter) is False
