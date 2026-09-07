import type { Metadata } from 'next'
import { LegalPage } from '@/components/LegalPage'
import { CONSENT } from '@/lib/legal'

export const metadata: Metadata = { title: CONSENT.title }

export default function ConsentPage() {
  return <LegalPage document={CONSENT} />
}
