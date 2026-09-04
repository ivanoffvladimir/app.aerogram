"""Разбор списка получателей из текста: построчный и табличный.

Оператор приносит список тем, чем умеет: вставкой из письма — по строке
на получателя, «город; адрес», — или файлом из учётной системы, где первая
строка называет столбцы. Оба разбираются здесь, **на сервере**, а не в
кабинете: разбор один на всех клиентов, включая машинных, и его можно
проверить на строках без браузера.

**Ни сети, ни базы.** Модуль возвращает то, что прочитал, а сопоставлять
с адресной книгой будет сервис: чистой функции легче доверять, и она
не знает, что такое тенант.

**Строка с ошибкой не выбрасывается молча.** Список на пятьсот получателей
без одного — это список на пятьсот, и потерявшийся узнает об этом, когда
не получит посылку. Поэтому такая строка называется по номеру, а оператор
решает сам: поправить или отказаться.

Формат таблицы угадывается по заголовку, а не объявляется: разделителем
считается тот из «;», «,» и табуляции, который даёт больше всего знакомых
названий столбцов. Названия принимаются русские и английские, потому что
файл приходит из чужой системы, и переименовывать в ней столбцы ради нас
никто не будет.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

__all__ = [
    "COLUMN_SYNONYMS",
    "ImportResult",
    "ImportedRow",
    "parse_recipients",
]

#: Разделитель старого, построчного формата: «город; адрес».
LINE_SEPARATOR: Final = ";"

#: Кандидаты в разделители табличного формата, в порядке предпочтения
#: при равном счёте. Табуляция первой: её не бывает внутри значений.
DELIMITERS: Final[tuple[str, ...]] = ("\t", ";", ",")

#: Названия столбцов → внутреннее поле. Сравнение после нормализации:
#: без регистра, пробелов, точек и подчёркиваний, «ё» как «е».
COLUMN_SYNONYMS: Final[dict[str, str]] = {
    # адрес
    "город": "city",
    "city": "city",
    "населенныйпункт": "city",
    "нп": "city",
    "адрес": "address_line",
    "address": "address_line",
    "addressline": "address_line",
    "улица": "street",
    "street": "street",
    "дом": "house",
    "house": "house",
    "квартира": "flat",
    "кв": "flat",
    "flat": "flat",
    "индекс": "postal_code",
    "почтовыйиндекс": "postal_code",
    "postalcode": "postal_code",
    "zip": "postal_code",
    "регион": "region",
    "область": "region",
    "region": "region",
    # ключ поиска в адресной книге
    "инн": "inn",
    "inn": "inn",
    "контрагент": "name",
    "название": "name",
    "наименование": "name",
    "получатель": "name",
    "организация": "name",
    "name": "name",
    "counterparty": "name",
    "company": "name",
    "recipient": "name",
    # груз этой строки
    "вес": "weight_kg",
    "вескг": "weight_kg",
    "weight": "weight_kg",
    "weightkg": "weight_kg",
    "ценность": "value_rub",
    "объявленнаяценность": "value_rub",
    "стоимость": "value_rub",
    "value": "value_rub",
    "declaredvalue": "value_rub",
}

_NORMALIZE_RE: Final = re.compile(r"[\s.,_\-()]+")


@dataclass(frozen=True, slots=True)
class ImportedRow:
    """Одна строка списка, как её прочитали.

    Либо адрес (город и строка адреса), либо ключ поиска в адресной книге
    (ИНН или название), либо и то и другое: тогда город сужает выбор адреса
    у найденного контрагента.
    """

    #: Номер строки в исходном тексте, как его показывает редактор оператора:
    #: с единицы и с учётом пустых. Иначе номер не найти глазами.
    line: int
    city: str | None = None
    address_line: str | None = None
    postal_code: str | None = None
    region: str | None = None
    inn: str | None = None
    name: str | None = None
    weight_kg: Decimal | None = None
    value_rub: Decimal | None = None

    @property
    def has_address(self) -> bool:
        return bool(self.city and self.address_line)

    @property
    def has_lookup_key(self) -> bool:
        return bool(self.inn or self.name)


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Что прочитано и что не удалось прочитать."""

    rows: tuple[ImportedRow, ...]
    errors: tuple[str, ...]
    #: Табличный формат с заголовком или построчный «город; адрес».
    tabular: bool


def normalize_header(cell: str) -> str:
    """Название столбца к виду, по которому ищется синоним.

    Запятая и скобки тоже отбрасываются: «Вес, кг» и «Ценность (руб)» —
    обычные заголовки выгрузки, и единица в них — пояснение, а не имя.
    """
    return _NORMALIZE_RE.sub("", cell.strip().lower().replace("ё", "е"))


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _decimal(value: str | None, *, what: str, line: int) -> tuple[Decimal | None, str | None]:
    """Число из ячейки. Запятая как десятичный разделитель принимается.

    Ошибка возвращается текстом, а не исключением: одна кривая ячейка
    не должна ронять разбор остальных строк.
    """
    text = _clean(value)
    if text is None:
        return None, None
    try:
        number = Decimal(text.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None, f"Строка {line}: {what} «{text}» не является числом"
    if number < 0:
        return None, f"Строка {line}: {what} не может быть отрицательным"
    return number, None


def _detect_header(first_line: str) -> tuple[str, dict[int, str]] | None:
    """Разделитель и раскладка столбцов по первой строке.

    ``None`` — первая строка не заголовок: ни одного знакомого названия
    ни при одном разделителе.
    """
    best: tuple[str, dict[int, str]] | None = None
    for delimiter in DELIMITERS:
        cells = next(csv.reader([first_line], delimiter=delimiter), [])
        layout = {
            index: COLUMN_SYNONYMS[key]
            for index, cell in enumerate(cells)
            if (key := normalize_header(cell)) in COLUMN_SYNONYMS
        }
        if layout and (best is None or len(layout) > len(best[1])):
            best = (delimiter, layout)
    return best


def parse_recipients(text: str) -> ImportResult:
    """Разобрать список получателей.

    Формат выбирается по первой непустой строке: если она называет хотя бы
    один знакомый столбец — это таблица, иначе каждая строка читается как
    «город; адрес».
    """
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = next(((i, line) for i, line in enumerate(lines, start=1) if line.strip()), None)
    if first is None:
        return ImportResult(rows=(), errors=(), tabular=False)

    header = _detect_header(first[1])
    if header is None:
        return _parse_lines(lines)
    delimiter, layout = header
    return _parse_table(lines, first_line=first[0], delimiter=delimiter, layout=layout)


def _parse_lines(lines: list[str]) -> ImportResult:
    """Построчный формат: «город; адрес». Первый разделитель — граница,
    остальные принадлежат адресу: «ул. Ленина, 1; кв. 5» — один адрес."""
    rows: list[ImportedRow] = []
    errors: list[str] = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        city, _, rest = stripped.partition(LINE_SEPARATOR)
        city_text = _clean(city)
        address = _clean(rest)
        if city_text is None or address is None:
            errors.append(f"Строка {number}: нужны город и адрес через «{LINE_SEPARATOR}»")
            continue
        rows.append(ImportedRow(line=number, city=city_text, address_line=address))
    return ImportResult(rows=tuple(rows), errors=tuple(errors), tabular=False)


def _parse_table(
    lines: list[str], *, first_line: int, delimiter: str, layout: dict[int, str]
) -> ImportResult:
    rows: list[ImportedRow] = []
    errors: list[str] = []
    body = lines[first_line:]
    reader = csv.reader(io.StringIO("\n".join(body)), delimiter=delimiter)
    for offset, cells in enumerate(reader, start=first_line + 1):
        if not any(cell.strip() for cell in cells):
            continue
        fields: dict[str, str | None] = {}
        for index, field in layout.items():
            fields[field] = _clean(cells[index]) if index < len(cells) else None

        address_line = fields.get("address_line")
        if address_line is None:
            # Улица, дом и квартира отдельными столбцами — частый вид выгрузки
            # из учётной системы. Склеиваются в одну строку адреса.
            parts = [fields.get("street"), fields.get("house")]
            if fields.get("flat"):
                parts.append(f"кв. {fields['flat']}")
            address_line = _clean(", ".join(part for part in parts if part))

        weight, weight_error = _decimal(fields.get("weight_kg"), what="вес", line=offset)
        value, value_error = _decimal(
            fields.get("value_rub"), what="объявленная ценность", line=offset
        )
        for error in (weight_error, value_error):
            if error:
                errors.append(error)
        if weight_error or value_error:
            continue

        row = ImportedRow(
            line=offset,
            city=fields.get("city"),
            address_line=address_line,
            postal_code=fields.get("postal_code"),
            region=fields.get("region"),
            inn=_digits_only(fields.get("inn")),
            name=fields.get("name"),
            weight_kg=weight,
            value_rub=value,
        )
        if not row.has_address and not row.has_lookup_key:
            errors.append(
                f"Строка {offset}: нужны либо город и адрес, либо ИНН или название "
                "контрагента из адресной книги"
            )
            continue
        rows.append(row)
    return ImportResult(rows=tuple(rows), errors=tuple(errors), tabular=True)


def _digits_only(value: str | None) -> str | None:
    """ИНН из ячейки: только цифры. Пробелы и апострофы из таблиц — не ИНН."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None
