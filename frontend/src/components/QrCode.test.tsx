import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { QrCode, qrModules } from './QrCode'

const OTPAUTH = 'otpauth://totp/Aerogram:a@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Aerogram'

/** Искатель — квадрат 7×7 в углу: тёмная рамка, светлое кольцо, тёмный центр 3×3. */
function isFinder(
  isDark: (r: number, c: number) => boolean,
  top: number,
  left: number,
): boolean {
  for (let r = 0; r < 7; r += 1) {
    for (let c = 0; c < 7; c += 1) {
      const ring = r === 0 || r === 6 || c === 0 || c === 6
      const core = r >= 2 && r <= 4 && c >= 2 && c <= 4
      if (isDark(top + r, left + c) !== (ring || core)) return false
    }
  }
  return true
}

describe('qrModules', () => {
  it('сторона — из ряда 21, 25, 29, … для версии, подобранной по данным', () => {
    const { count } = qrModules(OTPAUTH)
    expect(count).toBeGreaterThanOrEqual(21)
    expect((count - 21) % 4).toBe(0)
  })

  it('три искателя стоят по углам — то, по чему камера находит код', () => {
    // Без декодера это единственное, что тест может подтвердить: QR,
    // который выглядит правильно, но не читается, хуже его отсутствия.
    const { count, isDark } = qrModules(OTPAUTH)
    expect(isFinder(isDark, 0, 0)).toBe(true)
    expect(isFinder(isDark, 0, count - 7)).toBe(true)
    expect(isFinder(isDark, count - 7, 0)).toBe(true)
  })

  it('разные данные дают разные коды', () => {
    const a = qrModules(OTPAUTH)
    const b = qrModules(OTPAUTH.replace('JBSW', 'ABCD'))
    let differs = false
    for (let r = 0; r < a.count && !differs; r += 1) {
      for (let c = 0; c < a.count; c += 1) {
        if (a.isDark(r, c) !== b.isDark(r, c)) {
          differs = true
          break
        }
      }
    }
    expect(differs).toBe(true)
  })
})

describe('QrCode', () => {
  it('рисует картинку с подписью для незрячих, без innerHTML', () => {
    const { container } = render(<QrCode value={OTPAUTH} label="QR-код для приложения" />)
    expect(screen.getByRole('img', { name: 'QR-код для приложения' })).toBeInTheDocument()
    // Секрет в разметку не попадает: он закодирован в модули, а не написан.
    expect(container.innerHTML).not.toContain('JBSWY3DPEHPK3PXP')
    expect(container.querySelectorAll('rect').length).toBeGreaterThan(100)
  })
})
