# Команды разработки. Те же команды перечислены в CLAUDE.md §5 — при изменении
# правь оба места, иначе агент будет уверенно вызывать несуществующее.

.DEFAULT_GOAL := help
SHELL := /bin/bash

BACKEND := backend
FRONTEND := frontend
UV := uv --project $(BACKEND)

.PHONY: help
help:  ## Показать список команд
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: up
up:  ## Поднять локальное окружение (postgres, redis, mailpit, minio)
	docker compose up -d
	@echo "Postgres :5432 · Redis :6379 · Mailpit http://localhost:8025 · MinIO http://localhost:9001"

.PHONY: down
down:  ## Остановить локальное окружение
	docker compose down

.PHONY: install
install:  ## Установить зависимости backend и frontend
	$(UV) sync
	cd $(FRONTEND) && pnpm install

.PHONY: migrate
migrate:  ## Применить миграции
	cd $(BACKEND) && uv run alembic upgrade head

.PHONY: downgrade
downgrade:  ## Откатить последнюю миграцию (проверка обратимости)
	cd $(BACKEND) && uv run alembic downgrade -1

.PHONY: revision
revision:  ## Новая миграция: make revision m="описание". Автогенерация — ЧЕРНОВИК, читать построчно
	cd $(BACKEND) && uv run alembic revision --autogenerate -m "$(m)"

.PHONY: api
api:  ## Запустить API с автоперезагрузкой
	cd $(BACKEND) && uv run uvicorn aerogram.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: worker
worker:  ## Запустить Celery worker и beat
	cd $(BACKEND) && uv run celery -A aerogram.worker.app worker --beat --loglevel=info

.PHONY: front
front:  ## Запустить фронт
	cd $(FRONTEND) && pnpm dev

.PHONY: lint
lint:  ## ruff + mypy + import-linter + eslint
	cd $(BACKEND) && uv run ruff check src tests alembic
	cd $(BACKEND) && uv run ruff format --check src tests alembic
	cd $(BACKEND) && uv run mypy
	$(UV) run lint-imports --config .importlinter
	cd $(FRONTEND) && pnpm lint

.PHONY: format
format:  ## Отформатировать код
	cd $(BACKEND) && uv run ruff check --fix src tests alembic
	cd $(BACKEND) && uv run ruff format src tests alembic
	cd $(FRONTEND) && pnpm format

.PHONY: test
test:  ## Все тесты backend
	cd $(BACKEND) && uv run pytest

.PHONY: test-unit
test-unit:  ## Только юнит-тесты (не требуют БД)
	cd $(BACKEND) && uv run pytest tests/unit

.PHONY: test-front
test-front:  ## Тесты фронта
	cd $(FRONTEND) && pnpm test

.PHONY: test-e2e
test-e2e:  ## E2E фронта. Требует поднятых бэкенда и фронта
	cd $(FRONTEND) && pnpm test:e2e

.PHONY: openapi
openapi:  ## Сверить схему с контрактом и перегенерировать клиент фронта
	cd $(BACKEND) && uv run pytest tests/integration/test_openapi_conformance.py -q
	cd $(FRONTEND) && pnpm generate:api

.PHONY: security
security:  ## Проверка уязвимостей зависимостей
	cd $(BACKEND) && uv export --frozen --no-emit-project --format requirements-txt -o requirements-audit.txt
	cd $(BACKEND) && uv run pip-audit -r requirements-audit.txt --strict
	cd $(FRONTEND) && pnpm audit --audit-level=high

.PHONY: licenses
licenses:  ## Отчёт по лицензиям (несблокирующий, для due diligence)
	cd $(BACKEND) && uv run pip-licenses --format=markdown --output-file=../docs/licenses-backend.md
