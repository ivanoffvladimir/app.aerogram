/**
 * Области действия API-ключа. Порядок и подписи те же, что в `core/scopes.py`:
 * словарь закрытый, и выпуск с неизвестной областью сервер отклоняет.
 *
 * Дублирование сознательное: контракт этих путей не описывает, а показывать
 * владельцу `shipments:write` вместо «Создание и отмена отправлений» нельзя —
 * он выбирает права, а не читает наш код. Расхождение ловится тем, что сервер
 * отвечает 422 на неизвестную область.
 */
export const API_SCOPES: { value: string; label: string; hint: string }[] = [
  { value: 'rates:read', label: 'Расчёт и ранжирование', hint: 'Опрос перевозчиков и выдача' },
  { value: 'decisions:write', label: 'Фиксация решений', hint: 'Выбор варианта и override' },
  {
    value: 'shipments:read',
    label: 'Чтение отправлений и трекинга',
    hint: 'Список, карточка, лента событий',
  },
  {
    value: 'shipments:write',
    label: 'Создание и отмена отправлений',
    hint: 'Меняет заказ у перевозчика и стоит денег',
  },
  { value: 'carriers:read', label: 'Перевозчики и терминалы', hint: 'Справочник подключений' },
  {
    value: 'analytics:read',
    label: 'Аналитика и сводка',
    hint: 'Carrier Score, отчёт за период',
  },
  {
    value: 'directories:read',
    label: 'Справочники',
    hint: 'Нормализация адреса, города, контрагенты по ИНН',
  },
  { value: 'webhooks:read', label: 'Чтение подписок', hint: 'Список подписок на события' },
  {
    value: 'webhooks:write',
    label: 'Управление подписками',
    hint: 'Создание и удаление подписок',
  },
]

const BY_VALUE = new Map(API_SCOPES.map((scope) => [scope.value, scope]))

/** Подпись области. Неизвестная показывается кодом, а не исчезает из списка. */
export function scopeLabel(value: string): string {
  return BY_VALUE.get(value)?.label ?? value
}
