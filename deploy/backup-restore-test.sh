#!/usr/bin/env bash
# Ежемесячная проверка восстановления (RTO ≤ 4 часа).
#
# Задача в календаре. Проверка не декларируется, а выполняется: разворачиваем
# последний бэкап во временный кластер и убеждаемся, что данные на месте.
set -Eeuo pipefail

readonly RESTORE_DIR="${RESTORE_DIR:-/var/tmp/aerogram-restore-test}"
readonly RESTORE_PORT="${RESTORE_PORT:-5599}"

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }

cleanup() {
    pg_ctl -D "$RESTORE_DIR" stop --mode=immediate 2>/dev/null || true
    rm -rf "$RESTORE_DIR"
}
trap cleanup EXIT

main() {
    log "Восстановление последнего бэкапа в $RESTORE_DIR"
    rm -rf "$RESTORE_DIR"
    wal-g backup-fetch "$RESTORE_DIR" LATEST

    touch "$RESTORE_DIR/recovery.signal"
    pg_ctl -D "$RESTORE_DIR" -o "-p $RESTORE_PORT" -w start

    log "Проверка данных"
    local shipments
    shipments="$(psql -p "$RESTORE_PORT" -d aerogram -tAc 'SELECT count(*) FROM shipments')"
    log "Отправлений в восстановленной базе: $shipments"

    # Отсутствие таблицы или пустой ответ означают, что бэкап бесполезен.
    if [[ -z "$shipments" ]]; then
        log "ПРОВЕРКА НЕ ПРОЙДЕНА: не удалось прочитать данные"
        exit 1
    fi

    log "Проверка восстановления пройдена"
}

main "$@"
