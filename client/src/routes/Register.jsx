import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Button, ErrorBanner, Field, Panel, TextInput } from '../components/kit'
import { useAuth } from '../lib/authContext'

export default function Register() {
  const { register } = useAuth()
  const navigate = useNavigate()

  const [form, setForm] = useState({ full_name: '', email: '', password: '' })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  // Mirrors what identity-svc enforces. Checking here as well is not about
  // security -- it is about not making someone submit a form to learn a rule.
  const passwordProblem =
    form.password && form.password.length < 12
      ? 'Use at least 12 characters.'
      : null

  async function submit(event) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await register({ ...form, email: form.email.trim() })
      navigate('/onboarding', { replace: true })
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gate">
      <div className="gate__plate">
        <div className="gate__masthead">
          <span className="stamp-mark" style={{ margin: '0 auto' }} aria-hidden="true">
            IB
          </span>
          <div className="gate__name">Open an account</div>
          <div className="gate__tagline">Takes about two minutes</div>
        </div>
        <div className="gate__rule" />

        <Panel tone="framed">
          <form onSubmit={submit} className="stack">
            <Field label="Full name">
              {(id) => (
                <TextInput
                  id={id}
                  value={form.full_name}
                  autoComplete="name"
                  required
                  autoFocus
                  onChange={set('full_name')}
                />
              )}
            </Field>

            <Field label="Email">
              {(id) => (
                <TextInput
                  id={id}
                  type="email"
                  value={form.email}
                  autoComplete="username"
                  required
                  onChange={set('email')}
                />
              )}
            </Field>

            <Field label="Password" hint="12 characters minimum" error={passwordProblem}>
              {(id) => (
                <TextInput
                  id={id}
                  type="password"
                  value={form.password}
                  autoComplete="new-password"
                  required
                  minLength={12}
                  invalid={Boolean(passwordProblem)}
                  onChange={set('password')}
                />
              )}
            </Field>

            <ErrorBanner error={error} title="Could not create the account" />

            <Button
              type="submit"
              variant="primary"
              block
              busy={busy}
              disabled={Boolean(passwordProblem)}
            >
              Continue
            </Button>

            <p className="small muted" style={{ textAlign: 'center' }}>
              Already registered? <Link to="/login">Sign in</Link>
            </p>
          </form>
        </Panel>
      </div>
    </div>
  )
}
