import Link from 'next/link'
import type { LegalDocument } from '@/lib/legal'
import styles from './LegalPage.module.css'

/**
 * Юридический документ на публичной странице.
 *
 * Публичной намеренно: часть 2 статьи 18.1 закона № 152-ФЗ требует
 * неограниченного доступа к политике обработки. Документ за формой входа
 * этому требованию не отвечает — его не увидит тот, ради кого он написан.
 */
export function LegalPage({ document }: { document: LegalDocument }) {
  return (
    <main className={styles.page}>
      <Link className={styles.back} href="/login">
        ← Ко входу
      </Link>
      <h1>{document.title}</h1>

      {/* Черновик, выданный за действующий документ, хуже отсутствия
          документа: на него сошлются, а он не проверен. */}
      {document.version.startsWith('draft') && (
        <div className={styles.draft} role="note">
          Черновик. Текст выведен из фактического состава обработки и передач, но юридической
          проверки не проходил и действующей редакцией не является.
        </div>
      )}

      <div className={styles.meta}>
        Редакция {document.version} · действует с {document.effectiveFrom}
      </div>

      {document.sections.map((section) => (
        <section key={section.heading} className={styles.section}>
          <h2>{section.heading}</h2>
          {section.body?.map((paragraph) => (
            <p key={paragraph}>{paragraph}</p>
          ))}
          {section.items && (
            <ul>
              {section.items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </main>
  )
}
