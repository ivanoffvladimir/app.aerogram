/**
 * Показ Carrier Score: подписи, порядок и правило «не показывать число».
 *
 * Скор существует у перевозчика с первого дня работы клиента: он опирается
 * на платформенную базу и по мере накопления собственных отправлений
 * смещается к его личному опыту (ADR-0026). Поэтому рядом с числом всегда
 * стоит основание — иначе «87» у нового клиента читается как вывод из его
 * собственной статистики, которой ещё нет.
 *
 * Скор — непрозрачное число, пока рядом нет расшифровки (FR-7.5), поэтому
 * экран показывает составляющие и версию формулы. **Весов здесь нет
 * намеренно**: они живут на сервере (`intelligence/score.py`), и копия
 * на фронте разошлась бы с оригиналом молча — при следующем же пересмотре
 * весов, который обязан менять `formula_version`, а не подписи в кабинете.
 */

import type { CarrierAnalytics } from '@/api/client'

type Confidence = CarrierAnalytics['confidence']
type Basis = CarrierAnalytics['basis']

export const CONFIDENCE_LABELS: Record<Confidence, string> = {
  high: 'высокое',
  medium: 'среднее',
  low: 'низкое',
  insufficient: 'недостаточно данных',
}

/**
 * На чьих данных стоит число (ADR-0026).
 *
 * Подпись обязательна рядом со скором: оценка платформы и оценка по своим
 * отправлениям — разные утверждения, и клиент вправе знать, какое из них
 * перед ним. Без этого «87» у нового клиента выглядит как вывод из его
 * собственного опыта, которого не было.
 */
export const BASIS_LABELS: Record<Basis, string> = {
  platform: 'по платформе',
  mixed: 'ваши + платформа',
  own: 'по вашим отправлениям',
  none: '—',
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

/** Насколько скору можно верить, словами и с размером выборки.
 *
 * Выборка здесь всегда СВОЯ: доверие отвечает на вопрос «насколько это
 * про нас», и платформенная база его не повышает — она посчитана по чужим
 * договорам и чужим направлениям. Её размер показывается отдельно.
 */
export function confidenceText(row: CarrierAnalytics): string {
  const label = CONFIDENCE_LABELS[row.confidence]
  if (row.confidence === 'insufficient') {
    return row.sample_size > 0
      ? `${label}: ${row.sample_size} отправлений`
      : `${label}: отправлений ещё не было`
  }
  return `${label}, ваша выборка ${row.sample_size}`
}

/**
 * Пояснение под основанием: на скольких отправлениях стоит число.
 *
 * Числа клиентов платформы здесь нет и не будет: сколько компаний возит
 * этим перевозчиком — сведение о клиентской базе платформы, а не о нём.
 */
export function basisText(row: CarrierAnalytics): string {
  if (row.basis === 'none') return 'данных нет ни у вас, ни на платформе'
  if (row.basis === 'own') return `${row.sample_size} ваших отправлений`
  const platform = row.platform_sample_size ?? 0
  if (row.basis === 'platform') return `${platform} отправлений платформы, ваших пока нет`
  return `${row.sample_size} ваших и ${platform} по платформе`
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
