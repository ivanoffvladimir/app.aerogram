/**
 * Показ Carrier Score: подписи, порядок и правило «не показывать число».
 *
 * Скор — непрозрачное число, пока рядом нет расшифровки (FR-7.5), поэтому
 * экран показывает составляющие и версию формулы. **Весов здесь нет
 * намеренно**: они живут на сервере (`intelligence/score.py`), и копия
 * на фронте разошлась бы с оригиналом молча — при следующем же пересмотре
 * весов, который обязан менять `formula_version`, а не подписи в кабинете.
 */

import type { CarrierAnalytics } from '@/api/client'

type Confidence = CarrierAnalytics['confidence']

export const CONFIDENCE_LABELS: Record<Confidence, string> = {
  high: 'высокое',
  medium: 'среднее',
  low: 'низкое',
  insufficient: 'недостаточно данных',
}

export const SCOPE_LABELS: Record<string, string> = {
  global: 'по всем направлениям',
  direction: 'по направлению',
  direction_weight: 'по направлению и весу',
}

/**
 * Составляющие скора в порядке их веса в формуле.
 *
 * `higherIsBetter = false` у доли инцидентов: она единственная, где рост
 * значения — ухудшение. В формуле участвует обратная величина, и перепутать
 * их значит показать оператору «хорошо» там, где плохо.
 */
export const COMPONENTS = [
  { key: 'on_time_rate', label: 'В срок', higherIsBetter: true },
  { key: 'reliability', label: 'Надёжность оформления', higherIsBetter: true },
  { key: 'incident_rate', label: 'Инциденты', higherIsBetter: false },
  { key: 'price_index', label: 'Цена относительно медианы', higherIsBetter: true },
  { key: 'data_quality', label: 'Качество данных', higherIsBetter: true },
] as const

/**
 * Что показать вместо скора, когда его нет.
 *
 * `null` при `insufficient` — не ошибка, а обязательное поведение (FR-7.3):
 * ноль читался бы как «перевозчик плохой», а пустое место — как поломка
 * экрана. Ни то, ни другое не соответствует «мы ещё не знаем».
 */
export function scoreText(row: CarrierAnalytics): string {
  return row.score === null ? 'нет оценки' : String(row.score)
}

/** Насколько скору можно верить, словами и с размером выборки. */
export function confidenceText(row: CarrierAnalytics): string {
  const label = CONFIDENCE_LABELS[row.confidence]
  if (row.confidence === 'insufficient') {
    return row.sample_size > 0
      ? `${label}: ${row.sample_size} отправлений`
      : `${label}: отправлений ещё не было`
  }
  return `${label}, выборка ${row.sample_size}`
}

/**
 * Порядок строк: сначала оценённые, по убыванию скора.
 *
 * Перевозчики без оценки уходят вниз общей группой и внутри неё
 * упорядочены по имени. Иначе `null` попадает то в начало, то в конец
 * в зависимости от способа сравнения, и список выглядит случайным.
 */
export function byScore(rows: readonly CarrierAnalytics[]): CarrierAnalytics[] {
  return [...rows].sort((a, b) => {
    if (a.score === null && b.score === null) {
      return a.carrier_name.localeCompare(b.carrier_name, 'ru')
    }
    if (a.score === null) return 1
    if (b.score === null) return -1
    if (a.score !== b.score) return b.score - a.score
    return a.carrier_name.localeCompare(b.carrier_name, 'ru')
  })
}
