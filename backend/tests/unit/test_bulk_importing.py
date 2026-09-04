"""Разбор списка получателей: построчный и табличный форматы.

Ни сети, ни базы: модуль возвращает то, что прочитал. Сопоставление
с адресной книгой проверяется отдельно, интеграционно.
"""

from __future__ import annotations

from decimal import Decimal

from aerogram.bulk.importing import COLUMN_SYNONYMS, normalize_header, parse_recipients


class TestLineFormat:
    def test_city_and_address_per_line(self) -> None:
        result = parse_recipients("Москва; ул. Ленина, 1\nВладивосток; ул. Примерная, 2")
        assert not result.tabular
        assert result.errors == ()
        assert [(r.city, r.address_line) for r in result.rows] == [
            ("Москва", "ул. Ленина, 1"),
            ("Владивосток", "ул. Примерная, 2"),
        ]

    def test_the_first_separator_is_the_boundary(self) -> None:
        # Точка с запятой внутри адреса адресу и принадлежит.
        result = parse_recipients("Москва; ул. Ленина, 1; кв. 5")
        assert result.rows[0].address_line == "ул. Ленина, 1; кв. 5"

    def test_blank_lines_are_skipped_but_counted(self) -> None:
        # Номер строки — тот, что видит оператор в редакторе, с учётом пустых.
        result = parse_recipients("\nМосква; ул. Ленина, 1\n\nВладивосток")
        assert [r.line for r in result.rows] == [2]
        assert result.errors == ("Строка 4: нужны город и адрес через «;»",)

    def test_a_line_without_an_address_is_an_error_not_a_skip(self) -> None:
        # Молча выбросить получателя из рассылки хуже, чем отказаться считать.
        result = parse_recipients("Москва")
        assert result.rows == ()
        assert len(result.errors) == 1

    def test_windows_line_endings_and_bom(self) -> None:
        result = parse_recipients("﻿Москва; ул. Ленина, 1\r\nТверь; пр. Мира, 3\r\n")
        assert [r.city for r in result.rows] == ["Москва", "Тверь"]

    def test_empty_text_is_empty_not_an_error(self) -> None:
        result = parse_recipients("   \n\n")
        assert result.rows == () and result.errors == ()


class TestTableFormat:
    def test_a_header_switches_to_the_table(self) -> None:
        text = "Город;Адрес;Индекс\nМосква;ул. Ленина, 1;101000\nТверь;пр. Мира, 3;170000"
        result = parse_recipients(text)
        assert result.tabular
        assert [(r.city, r.address_line, r.postal_code) for r in result.rows] == [
            ("Москва", "ул. Ленина, 1", "101000"),
            ("Тверь", "пр. Мира, 3", "170000"),
        ]
        assert [r.line for r in result.rows] == [2, 3]

    def test_the_delimiter_is_the_one_that_names_the_most_columns(self) -> None:
        # Запятая внутри адреса не делает запятую разделителем: с ней
        # заголовок узнаётся хуже, чем с точкой с запятой.
        text = "город;адрес\nМосква;ул. Ленина, д. 1, кв. 5"
        result = parse_recipients(text)
        assert result.rows[0].address_line == "ул. Ленина, д. 1, кв. 5"

    def test_tab_separated_export(self) -> None:
        text = "ИНН\tНазвание\tГород\n7701234567\tООО Роспломба\tМосква"
        result = parse_recipients(text)
        row = result.rows[0]
        assert (row.inn, row.name, row.city) == ("7701234567", "ООО Роспломба", "Москва")
        assert row.has_lookup_key and not row.has_address

    def test_quoted_cells_with_the_delimiter_inside(self) -> None:
        text = 'city,address\nМосква,"ул. Ленина, 1"'
        result = parse_recipients(text)
        assert result.rows[0].address_line == "ул. Ленина, 1"

    def test_street_house_and_flat_are_joined_into_one_line(self) -> None:
        text = "Город;Улица;Дом;Кв\nМосква;ул. Ленина;1;5"
        result = parse_recipients(text)
        assert result.rows[0].address_line == "ул. Ленина, 1, кв. 5"

    def test_header_names_are_forgiving(self) -> None:
        # Регистр, пробелы, точки и «ё» — не повод не узнать столбец.
        for raw, expected in (
            ("Почтовый индекс", "postal_code"),
            ("Address_Line", "address_line"),
            ("НАСЕЛЁННЫЙ ПУНКТ", "city"),
            ("вес, кг", "weight_kg"),
        ):
            assert COLUMN_SYNONYMS[normalize_header(raw)] == expected, raw

    def test_unknown_columns_are_ignored(self) -> None:
        text = "Менеджер;Город;Адрес;Примечание\nПетров;Москва;ул. Ленина, 1;срочно"
        result = parse_recipients(text)
        assert (result.rows[0].city, result.rows[0].address_line) == ("Москва", "ул. Ленина, 1")

    def test_inn_keeps_digits_only(self) -> None:
        # Апострофы и пробелы, которыми таблицы защищают ведущие нули, — не ИНН.
        text = "инн\n'7701234567\n77 0123 4567"
        result = parse_recipients(text)
        assert [r.inn for r in result.rows] == ["7701234567", "7701234567"]

    def test_a_row_with_neither_address_nor_key_is_an_error(self) -> None:
        text = "Город;Адрес;ИНН\n;;\nМосква;;"
        result = parse_recipients(text)
        assert result.rows == ()
        assert result.errors == (
            "Строка 3: нужны либо город и адрес, либо ИНН или название контрагента "
            "из адресной книги",
        )


class TestPerRowCargo:
    def test_weight_and_value_are_read_as_decimals(self) -> None:
        # Запятая как десятичный разделитель — так пишут в русских таблицах.
        text = "Город;Адрес;Вес;Ценность\nМосква;ул. Ленина, 1;1,5;12 000"
        row = parse_recipients(text).rows[0]
        assert row.weight_kg == Decimal("1.5")
        assert row.value_rub == Decimal("12000")

    def test_a_non_number_names_its_line_and_drops_the_row(self) -> None:
        text = "Город;Адрес;Вес\nМосква;ул. Ленина, 1;тяжёлый\nТверь;пр. Мира, 3;2"
        result = parse_recipients(text)
        assert result.errors == ("Строка 2: вес «тяжёлый» не является числом",)
        assert [r.city for r in result.rows] == ["Тверь"]

    def test_negative_values_are_refused(self) -> None:
        text = "Город;Адрес;Ценность\nМосква;ул. Ленина, 1;-5"
        result = parse_recipients(text)
        assert result.errors == ("Строка 2: объявленная ценность не может быть отрицательным",)

    def test_empty_cargo_cells_mean_the_common_cargo(self) -> None:
        text = "Город;Адрес;Вес\nМосква;ул. Ленина, 1;"
        row = parse_recipients(text).rows[0]
        assert row.weight_kg is None
