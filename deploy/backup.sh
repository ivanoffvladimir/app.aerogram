#!/usr/bin/env bash
# Ежесуточный полный бэкап + непрерывная архивация WAL в S3 (RPO ≤ 1 час).
#
# Восстановление проверяется раз в месяц скриптом backup-restore-test.sh.
# Бэкап, который ни разу не восстанавливали, бэкапом не является.
set -Eeuo pipefail

readonly RETENTION_DAYS="${RETENTION_DAYS:-30}"

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }

notify_failure() {
    local message="$1"
    log "ОШИБКА: $message"
    # Ошибка ночного бэкапа — алерт в Telegram немедленно (раздел 12 ТЗ).
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ALERT_CHAT_ID:-}" ]]; then
        curl --silent --max-time 10 \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_ALERT_CHAT_ID}" \
            -d "text=Ошибка ночного бэкапа: ${message}" >/dev/null || true
    fi
}

trap 'notify_failure "скрипт завершился с ошибкой на строке $LINENO"' ERR

main() {
    log "Полный бэкап"
    wal-g backup-push "${PGDATA:-/var/lib/postgresql/data}"

    log "Удаление бэкапов старше $RETENTION_DAYS суток"
    wal-g delete retain FULL "$RETENTION_DAYS" --confirm

    log "Бэкап завершён"
}

main "$@"
