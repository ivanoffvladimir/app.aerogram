"""DTO справочников: ответ ДаData, города, терминалы, сопоставления.

Имена полей ``DadataAddressData`` повторяют имена ДаData дословно. Это
сознательно: любое переименование пришлось бы держать в голове при каждом
чтении документации перевозчика данных, а выигрыша не даёт.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AddressNormalizeRequest",
    "CityMappingConfirm",
    "CityMappingQueueItem",
    "CityOut",
    "CitySuggestResponse",
    "CitySuggestion",
    "DadataAddressData",
    "DadataSuggestion",
    "NormalizedAddress",
    "PartyDraft",
    "PartyLookupRequest",
    "TerminalListResponse",
    "TerminalOut",
]


class DadataAddressData(BaseModel):
    """Блок ``data`` одной подсказки или результата стандартизации ДаData.

    Модель намеренно нестрогая по составу: ДаData добавляет поля без
    предупреждения, и падать на незнакомом поле нельзя — форма ввода адреса
    перестала бы работать из-за чужого релиза.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    country: str | None = None
    country_iso_code: str | None = None

    region: str | None = None
    region_with_type: str | None = None
    region_fias_id: str | None = None
    region_kladr_id: str | None = None

    area: str | None = None
    area_with_type: str | None = None
    area_fias_id: str | None = None

    city: str | None = None
    city_with_type: str | None = None
    city_fias_id: str | None = None
    city_kladr_id: str | None = None

    settlement: str | None = None
    settlement_with_type: str | None = None
    settlement_fias_id: str | None = None
    settlement_kladr_id: str | None = None

    street: str | None = None
    street_with_type: str | None = None
    house: str | None = None
    block: str | None = None
    flat: str | None = None
    postal_box: str | None = None
    postal_code: str | None = None

    fias_id: str | None = None
    fias_level: str | None = None
    kladr_id: str | None = None

    geo_lat: str | None = None
    geo_lon: str | None = None
    timezone: str | None = None

    #: Коды качества стандартизации. В подсказках отсутствуют.
    qc: str | None = None
    qc_complete: str | None = None
    qc_house: str | None = None
    qc_geo: str | None = None


class DadataSuggestion(BaseModel):
    """Одна подсказка ДаData."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    value: str
    unrestricted_value: str | None = None
    data: DadataAddressData


class CitySuggestion(BaseModel):
    """Подсказка города, приведённая к нашей модели."""

    fias_id: str
    parent_fias_id: str | None = None
    fias_level: int
    name: str
    full_name: str
    region: str | None = None
    kladr_id: str | None = None
    postal_code: str | None = None
    timezone: str | None = None


class CitySuggestResponse(BaseModel):
    """Ответ подсказок города.

    ``degraded`` — признак того, что подсказки пришли из локального справочника,
    а не от ДаData. Ответ остаётся успешным: подсказка вспомогательна, и её сбой
    не должен останавливать создание отправления (раздел 3 ТЗ по реализации).
    """

    items: list[CitySuggestion]
    degraded: bool = False
    degraded_reason: str | None = None


class CityOut(BaseModel):
    """Город справочника."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fias_id: str
    name: str
    full_name: str | None = None
    region: str | None = None
    fias_level: int | None = None
    kladr_id: str | None = None
    postal_code: str | None = None
    timezone: str | None = None
    lat: float | None = None
    lon: float | None = None


class AddressNormalizeRequest(BaseModel):
    """Запрос нормализации адреса, введённого строкой."""

    query: str = Field(min_length=1, max_length=300)


class NormalizedAddress(BaseModel):
    """Результат нормализации адреса.

    ``fitness`` отвечает на вопрос «для чего этот адрес годится», а не
    «валиден ли он»: для расчёта и доставки до пункта выдачи дом не нужен,
    для доставки до двери обязателен.
    """

    city_fias_id: str | None = None
    city_parent_fias_id: str | None = None
    city_name: str | None = None
    region: str | None = None
    postal_code: str | None = None
    street: str | None = None
    house: str | None = None
    flat: str | None = None
    lat: float | None = None
    lon: float | None = None
    #: Точность координат: ``house``, ``nearest_house``, ``street``,
    #: ``settlement``, ``city``. Пусто, если координат нет.
    geo_precision: str | None = None
    fitness: str
    blockers: list[str] = Field(default_factory=list)
    degraded: bool = False


class TerminalOut(BaseModel):
    """Терминал или пункт выдачи перевозчика."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    carrier_id: UUID
    external_code: str
    city_fias_id: str | None = None
    city_name: str | None = None
    address: str | None = None
    type: str
    work_hours: str | None = None
    lat: float | None = None
    lon: float | None = None
    has_cash: bool
    has_card: bool
    is_active: bool


class TerminalListResponse(BaseModel):
    """Постраничный список терминалов."""

    items: list[TerminalOut]
    total: int
    limit: int
    offset: int


class CityMappingQueueItem(BaseModel):
    """Строка очереди ручного сопоставления городов (FR-8.2, FR-12.3)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    carrier_id: UUID
    carrier_city_code: str
    carrier_city_name: str | None = None
    carrier_region_name: str | None = None
    reason: str
    candidates: list[dict[str, object]] = Field(default_factory=list)
    best_score: float | None = None
    resolved_at: object | None = None


class CityMappingConfirm(BaseModel):
    """Подтверждение сопоставления администратором платформы."""

    city_fias_id: str = Field(min_length=36, max_length=36)


class PartyLookupRequest(BaseModel):
    """Поиск организации по ИНН для заполнения контрагента (FR-8.4)."""

    inn: str = Field(min_length=10, max_length=12, pattern=r"^\d+$")
    kpp: str | None = Field(default=None, min_length=9, max_length=9, pattern=r"^\d+$")


class PartyDraft(BaseModel):
    """Черновик контрагента, полученный по ИНН.

    Возвращается именно черновиком, а не создаётся сразу: решение о заведении
    контрагента принимает пользователь, и данные реестра он должен увидеть
    до сохранения.
    """

    type: str
    name: str
    inn: str
    kpp: str | None = None
    address: str | None = None
    city_fias_id: str | None = None
    is_active: bool = True
