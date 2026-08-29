-- Инициализация ролей БД.
--
-- ТРЕБОВАНИЕ К БАЗЕ: она должна быть создана с UTF-8-локалью, например
--     CREATE DATABASE aerogram TEMPLATE template0 LOCALE 'C.UTF-8' ENCODING 'UTF8';
-- В локали C PostgreSQL не сворачивает регистр кириллицы, и поиск контрагента
-- по названию перестаёт работать без единой ошибки в логах. Требование
-- проверяется тестом TestDatabaseLocale, а не остаётся на словах.
--
-- Роль приложения НЕ имеет атрибута BYPASSRLS — на этом держится вся изоляция
-- тенантов (раздел 7.2 ТЗ). Тест test_tenant_isolation.py это проверяет.
-- Пароль здесь только для локальной разработки; в проде роль создаётся отдельно,
-- пароль приходит из зашифрованного .env.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aerogram_app') THEN
        CREATE ROLE aerogram_app LOGIN PASSWORD 'app' NOBYPASSRLS;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE aerogram TO aerogram_app;
GRANT USAGE ON SCHEMA public TO aerogram_app;
