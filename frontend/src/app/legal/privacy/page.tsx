import type { Metadata } from 'next'
import { LegalPage } from '@/components/LegalPage'
import { PRIVACY_POLICY } from '@/lib/legal'

export const metadata: Metadata = { title: PRIVACY_POLICY.title }

export default function PrivacyPolicyPage() {
  return <LegalPage document={PRIVACY_POLICY} />
}
