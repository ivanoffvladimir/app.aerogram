#!/usr/bin/env bash
# Развёртывание: образы → миграции → перезапуск → health check → откат при неуспехе.
#
# Миграции выполняются ОТДЕЛЬНЫМ шагом, до перезапуска приложения (раздел 7.4 ТЗ, п. 4).
set -Eeuo pipefail

readonly COMPOSE_FILE="${COMPOSE_FILE:-compose.prod.yaml}"
readonly HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
readonly HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
readonly TAG="${TAG:?укажите TAG образа для развёртывания}"

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }

previous_tag() {
    docker compose -f "$COMPOSE_FILE" images --format json app 2>/dev/null \
        | grep -o '"Tag":"[^"]*"' | head -1 | cut -d'"' -f4 || echo ""
}

rollback() {
    local previous="$1"
    if [[ -z "$previous" ]]; then
        log "ОТКАТ НЕВОЗМОЖЕН: предыдущий тег неизвестен. Требуется ручное вмешательство."
        return 1
    fi
    log "Откат на тег $previous"
    TAG="$previous" docker compose -f "$COMPOSE_FILE" up -d app worker
}

main() {
    local previous
    previous="$(previous_tag)"
    log "Текущий тег: ${previous:-неизвестен}. Разворачиваем: $TAG"

    log "Получение образов"
    TAG="$TAG" docker compose -f "$COMPOSE_FILE" pull app worker

    log "Миграции"
    TAG="$TAG" docker compose -f "$COMPOSE_FILE" run --rm app alembic upgrade head

    log "Перезапуск приложения и воркера"
    TAG="$TAG" docker compose -f "$COMPOSE_FILE" up -d app worker

    log "Проверка готовности"
    for _ in $(seq 1 "$HEALTH_RETRIES"); do
        if curl --fail --silent --max-time 5 "$HEALTH_URL" >/dev/null; then
            log "Развёртывание успешно: $TAG"
            return 0
        fi
        sleep 2
    done

    log "Health check не прошёл за $((HEALTH_RETRIES * 2)) секунд"
    rollback "$previous"
    return 1
}

main "$@"
