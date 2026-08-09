import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Page } from '../components/AppShell'
import {
  Banner,
  Button,
  ErrorBanner,
  Field,
  KeyValue,
  Panel,
  Select,
  Stamp,
  TextInput,
} from '../components/kit'
import DocumentUpload from '../components/DocumentUpload'
import { api } from '../lib/api'
import { formatDateTime, humanise } from '../lib/format'

/**
 * UC1: apply, KYC runs, eligibility decides, an account opens.
 *
 * The whole thing after submit is asynchronous across three services, so the
 * screen polls and narrates. Showing the state machine rather than a spinner
 * is the point: an application that lands in MANUAL_REVIEW is not stuck, and
 * the customer should be able to see why.
 */

const STAGES = [
  { status: 'DRAFT', label: 'Details' },
  { status: 'SUBMITTED', label: 'Submitted' },
  { status: 'KYC_PENDING', label: 'Identity checks' },
  { status: 'KYC_PASSED', label: 'Verified' },
  { status: 'ELIGIBILITY_PASSED', label: 'Eligibility' },
  { status: 'ACCOUNT_OPENED', label: 'Account open' },
]

const TERMINAL_BAD = new Set(['KYC_FAILED', 'REJECTED'])

function Track({ status }) {
  const index = STAGES.findIndex((stage) => stage.status === status)
  const failed = TERMINAL_BAD.has(status)
  const review = status === 'MANUAL_REVIEW'

  return (
    <ol
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${STAGES.length}, 1fr)`,
        gap: 2,
        listStyle: 'none',
        padding: 0,
        margin: 0,
      }}
    >
      {STAGES.map((stage, position) => {
        const done = index >= position && index !== -1
        const background = failed
          ? 'var(--block)'
          : review
            ? 'var(--review)'
            : 'var(--allow)'
        return (
          <li key={stage.status}>
            <div
              style={{
                height: 6,
                background: done ? background : 'var(--surface-sunk)',
                border: '1px solid var(--rule-strong)',
              }}
            />
            <div className="tiny" style={{ marginTop: 6, color: done ? 'var(--ink)' : 'var(--ink-ghost)' }}>
              {stage.label}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

export default function Onboarding() {
  const navigate = useNavigate()
  const [applicationId, setApplicationId] = useState(
    () => sessionStorage.getItem('ind.application') || null,
  )
  const [form, setForm] = useState({
    full_name: '',
    date_of_birth: '',
    national_id: '',
    nationality: 'IN',
    employment_status: 'EMPLOYED',
    annual_income: '600000',
  })
  const [documents, setDocuments] = useState([])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  const { data: application } = useQuery({
    queryKey: ['application', applicationId],
    queryFn: () => api.get(`/api/onboarding/applications/${applicationId}`),
    enabled: Boolean(applicationId),
    // Stop polling once the machine reaches a state that only a human moves.
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (!status) return 2000
      const settled = ['ACCOUNT_OPENED', 'KYC_FAILED', 'REJECTED', 'MANUAL_REVIEW']
      return settled.includes(status) ? false : 2000
    },
  })

  useEffect(() => {
    if (application?.status === 'ACCOUNT_OPENED') {
      sessionStorage.removeItem('ind.application')
      const timer = setTimeout(() => navigate('/accounts'), 2200)
      return () => clearTimeout(timer)
    }
  }, [application?.status, navigate])

  async function submit(event) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const created = await api.post('/api/onboarding/applications', {
        customer_info: {
          ...form,
          annual_income: Number(form.annual_income),
          documents,
        },
      })
      await api.post(`/api/onboarding/applications/${created.id}/submit`, {})
      sessionStorage.setItem('ind.application', created.id)
      setApplicationId(created.id)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  if (applicationId && application) {
    const { status, eligibility, account, timeline = [] } = application
    return (
      <Page
        title="Your application"
        subtitle="Identity checks run across our verification partners. This page updates itself."
      >
        <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
          <Panel tone="raised" ticks className="reveal">
            <Track status={status} />
            <div className="row row--between" style={{ marginTop: 'var(--s-5)' }}>
              <Stamp
                tone={
                  status === 'ACCOUNT_OPENED'
                    ? 'allow'
                    : TERMINAL_BAD.has(status)
                      ? 'block'
                      : status === 'MANUAL_REVIEW'
                        ? 'review'
                        : 'live'
                }
                working={!['ACCOUNT_OPENED', 'KYC_FAILED', 'REJECTED', 'MANUAL_REVIEW'].includes(status)}
              >
                {humanise(status)}
              </Stamp>
              {application.status_reason && (
                <span className="small muted">{application.status_reason}</span>
              )}
            </div>
          </Panel>

          {status === 'ACCOUNT_OPENED' && account && (
            <Banner tone="ok" title="Account opened">
              Your account number is <strong className="mono">{account.account_number}</strong>.
              Taking you to your dashboard…
            </Banner>
          )}

          {status === 'MANUAL_REVIEW' && (
            <Banner tone="warn" title="With our review team">
              Something in your application needs a person to look at it. We will
              email you — there is nothing further for you to do.
            </Banner>
          )}

          {TERMINAL_BAD.has(status) && (
            <Banner tone="error" title="We could not proceed">
              {application.status_reason || 'Your application was not successful.'}
            </Banner>
          )}

          {eligibility && (
            <Panel title="Eligibility assessment" className="reveal">
              <KeyValue
                rows={[
                  ['Decision', <Stamp key="d" tone={eligibility.decision === 'PASS' ? 'allow' : 'review'}>{eligibility.decision}</Stamp>],
                  ['Risk score', <span key="r" className="mono">{eligibility.risk_score} / 100</span>],
                  ['Assigned tier', eligibility.tier],
                  ['Policy version', <span key="p" className="mono tiny">{eligibility.policy_version}</span>],
                ]}
              />
              {Array.isArray(eligibility.factors) && eligibility.factors.length > 0 && (
                <ul className="small muted" style={{ marginTop: 'var(--s-4)', paddingLeft: 18 }}>
                  {eligibility.factors.map((factor, index) => (
                    <li key={index}>{typeof factor === 'string' ? factor : JSON.stringify(factor)}</li>
                  ))}
                </ul>
              )}
            </Panel>
          )}

          <Panel title="Progress" flush className="reveal">
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Stage</th>
                    <th>When</th>
                    <th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {timeline.map((entry, index) => (
                    <tr key={index}>
                      <td>{humanise(entry.status)}</td>
                      <td className="mono tiny">{formatDateTime(entry.at)}</td>
                      <td className="muted">{entry.reason || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>
      </Page>
    )
  }

  return (
    <Page
      title="Open your account"
      subtitle="We verify your identity against sanctions, PEP and document checks before opening an account."
    >
      <div style={{ maxWidth: 620 }}>
        <Panel tone="raised" ticks>
          <form onSubmit={submit} className="stack">
            <Field label="Full legal name" hint="As printed on your ID">
              {(id) => <TextInput id={id} value={form.full_name} required onChange={set('full_name')} />}
            </Field>

            <div className="grid-2">
              <Field label="Date of birth">
                {(id) => (
                  <TextInput
                    id={id}
                    type="date"
                    value={form.date_of_birth}
                    required
                    onChange={set('date_of_birth')}
                  />
                )}
              </Field>
              <Field label="Nationality">
                {(id) => (
                  <Select
                    id={id}
                    value={form.nationality}
                    onChange={set('nationality')}
                    options={[
                      { value: 'IN', label: 'India' },
                      { value: 'SG', label: 'Singapore' },
                      { value: 'AE', label: 'United Arab Emirates' },
                      { value: 'GB', label: 'United Kingdom' },
                      { value: 'US', label: 'United States' },
                    ]}
                  />
                )}
              </Field>
            </div>

            <Field label="National ID / PAN" hint="Encrypted and held by our KYC service only">
              {(id) => (
                <TextInput
                  id={id}
                  mono
                  value={form.national_id}
                  required
                  placeholder="ABCDE1234F"
                  onChange={set('national_id')}
                />
              )}
            </Field>

            <div className="grid-2">
              <Field label="Employment">
                {(id) => (
                  <Select
                    id={id}
                    value={form.employment_status}
                    onChange={set('employment_status')}
                    options={[
                      { value: 'EMPLOYED', label: 'Employed' },
                      { value: 'SELF_EMPLOYED', label: 'Self-employed' },
                      { value: 'STUDENT', label: 'Student' },
                      { value: 'RETIRED', label: 'Retired' },
                      { value: 'UNEMPLOYED', label: 'Not working' },
                    ]}
                  />
                )}
              </Field>
              <Field label="Annual income" hint="INR">
                {(id) => (
                  <TextInput
                    id={id}
                    mono
                    inputMode="numeric"
                    value={form.annual_income}
                    onChange={set('annual_income')}
                  />
                )}
              </Field>
            </div>

            <DocumentUpload documents={documents} onChange={setDocuments} />

            <ErrorBanner error={error} title="Could not submit" />

            {documents.length === 0 ? (
              <Banner tone="warn" title="No documents attached">
                We can still take your application, but without documents it goes to
                a person to review rather than opening straight away.
              </Banner>
            ) : (
              <Banner tone="info" title="What happens next">
                We run sanctions, PEP and document checks, then score your
                application. Most accounts open within a minute.
              </Banner>
            )}

            <Button type="submit" variant="primary" busy={busy}>
              Submit application
            </Button>
          </form>
        </Panel>
      </div>
    </Page>
  )
}
