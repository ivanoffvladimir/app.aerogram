"""Производственный календарь РФ (FR-6.2)."""

from __future__ import annotations

from datetime import date

from aerogram.shared.calendar_ru import load_calendar


class TestWorkingDays:
    def test_weekend_is_not_working(self) -> None:
        calendar = load_calendar()
        assert calendar.is_working_day(date(2026, 8, 29)) is False  # суббота
        assert calendar.is_working_day(date(2026, 8, 30)) is False  # воскресенье

    def test_weekday_is_working(self) -> None:
        assert load_calendar().is_working_day(date(2026, 8, 28)) is True  # пятница

    def test_statutory_holiday_is_not_working(self) -> None:
        calendar = load_calendar()
        # Нерабочие праздничные дни по ТК РФ, ст. 112 — действуют в любой год.
        assert calendar.is_working_day(date(2026, 1, 5)) is False
        assert calendar.is_working_day(date(2026, 5, 9)) is False
        assert calendar.is_working_day(date(2026, 11, 4)) is False


class TestBusinessDaysBetween:
    def test_counts_half_open_interval(self) -> None:
        # Понедельник → пятница: день приёма не входит, день доставки входит.
        calendar = load_calendar()
        assert calendar.business_days_between(date(2026, 8, 24), date(2026, 8, 28)) == 4

    def test_skips_weekend(self) -> None:
        # Пятница → понедельник: один рабочий день, а не три календарных.
        calendar = load_calendar()
        assert calendar.business_days_between(date(2026, 8, 28), date(2026, 8, 31)) == 1

    def test_same_day_is_zero(self) -> None:
        calendar = load_calendar()
        assert calendar.business_days_between(date(2026, 8, 28), date(2026, 8, 28)) == 0

    def test_negative_when_end_before_start(self) -> None:
        # Регресс дат встречается в данных перевозчиков; молчать об этом нельзя.
        calendar = load_calendar()
        assert calendar.business_days_between(date(2026, 8, 28), date(2026, 8, 24)) == -4

    def test_new_year_holidays_do_not_count(self) -> None:
        # 30 декабря → 11 января: каникулы не делают перевозчика нарушителем SLA.
        calendar = load_calendar()
        assert calendar.business_days_between(date(2025, 12, 30), date(2026, 1, 12)) == 3


class TestAddBusinessDays:
    def test_skips_weekend(self) -> None:
        calendar = load_calendar()
        assert calendar.add_business_days(date(2026, 8, 28), 1) == date(2026, 8, 31)


class TestVerification:
    def test_current_year_verification_status_is_explicit(self) -> None:
        """Календарь обязан честно сообщать, сверен ли год с постановлением.

        Пока год не сверен, расчёт идёт по базовым праздникам без переносов —
        это допустимо, но об этом должно быть известно, а не выясняться из отчёта.
        """
        calendar = load_calendar()
        assert isinstance(calendar.is_verified(2026), bool)
