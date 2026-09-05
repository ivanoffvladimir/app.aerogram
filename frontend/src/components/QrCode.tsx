import qrcode from 'qrcode-generator'
import { useMemo } from 'react'

interface Props {
  /** Что кодировать. Для второго фактора — ссылка `otpauth://` с секретом. */
  value: string
  /** Подпись для незрячих: сама картинка им ничего не скажет. */
  label: string
  /** Сторона картинки в пикселях. */
  size?: number
}

/** Тихая зона по стандарту: четыре модуля с каждой стороны. */
const QUIET_ZONE = 4

/**
 * Модули QR-кода: сторона и функция «тёмный ли модуль».
 *
 * Отдельно от компонента, чтобы проверяться без DOM: неверный QR выглядит
 * правильно, и единственное, что тест может подтвердить без декодера, —
 * структурные инварианты (размер, искатели по углам).
 */
export function qrModules(value: string): {
  count: number
  isDark: (r: number, c: number) => boolean
} {
  // Тип 0 — размер подбирается по длине данных. Уровень M — то, что ставят
  // приложения-аутентификаторы по умолчанию: экран не бумага, царапин нет.
  const qr = qrcode(0, 'M')
  qr.addData(value, 'Byte')
  qr.make()
  return { count: qr.getModuleCount(), isDark: (r, c) => qr.isDark(r, c) }
}

/**
 * QR-код как SVG, собранный из прямоугольников в дереве React.
 *
 * Библиотека умеет отдать готовую SVG-строку, но вставлять её значило бы
 * `dangerouslySetInnerHTML`; вместо этого модули кладутся в дерево React,
 * и кодируемая строка с секретом в DOM не попадает вовсе (ADR-0024).
 */
export function QrCode({ value, label, size = 192 }: Props) {
  const { count, isDark } = useMemo(() => qrModules(value), [value])
  const side = count + QUIET_ZONE * 2
  const cells: { key: string; x: number; y: number }[] = []
  for (let r = 0; r < count; r += 1) {
    for (let c = 0; c < count; c += 1) {
      if (isDark(r, c)) cells.push({ key: `${r}-${c}`, x: c + QUIET_ZONE, y: r + QUIET_ZONE })
    }
  }
  return (
    <svg
      role="img"
      aria-label={label}
      width={size}
      height={size}
      viewBox={`0 0 ${side} ${side}`}
      shapeRendering="crispEdges"
    >
      <rect width={side} height={side} fill="#fff" />
      {cells.map((cell) => (
        <rect key={cell.key} x={cell.x} y={cell.y} width={1} height={1} fill="#000" />
      ))}
    </svg>
  )
}
