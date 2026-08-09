import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Button, ErrorBanner, Field, Panel, TextInput } from '../components/kit'
import { useAuth } from '../lib/authContext'
import { HOME_FOR_ROLE } from '../lib/roles'

export default function Login() {
  const { signIn, user, ready, role } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  if (ready && user) {
    return <Navigate to={location.state?.from ?? HOME_FOR_ROLE[role] ?? '/'} replace />
  }

  async function submit(event) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const profile = await signIn(email.trim(), password)
      navigate(location.state?.from ?? HOME_FOR_ROLE[profile.role] ?? '/', { replace: true })
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
          <div className="gate__name">IND Bank</div>
          <div className="gate__tagline">Payments &amp; Fraud Operations</div>
        </div>
        <div className="gate__rule" />

        <Panel tone="framed">
          <form onSubmit={submit} className="stack">
            <Field label="Email">
              {(id, invalid) => (
                <TextInput
                  id={id}
                  type="email"
                  value={email}
                  autoComplete="username"
                  autoFocus
                  required
                  invalid={invalid}
                  onChange={(event) => setEmail(event.target.value)}
                />
              )}
            </Field>

            <Field label="Password">
              {(id) => (
                <TextInput
                  id={id}
                  type="password"
                  value={password}
                  autoComplete="current-password"
                  required
                  onChange={(event) => setPassword(event.target.value)}
                />
              )}
            </Field>

            <ErrorBanner error={error} title="Could not sign in" />

            <Button type="submit" variant="primary" block busy={busy}>
              Sign in
            </Button>

            <p className="small muted" style={{ textAlign: 'center' }}>
              New to IND Bank? <Link to="/register">Open an account</Link>
            </p>
          </form>
        </Panel>

        <div
          className="panel panel--framed"
          style={{ borderTop: 'none', padding: 'var(--s-3) var(--s-4)' }}
        >
          <div className="label">Demo sign-ins</div>
          <div className="tiny muted mono" style={{ marginTop: 8, lineHeight: 1.7 }}>
            asha@indbank.test — customer
            <br />
            analyst@indbank.test — fraud analyst
            <br />
            ops@indbank.test — operations
            <br />
            admin@indbank.test — administrator
            <br />
            <span className="faint">password for all: demo-password-2026</span>
          </div>
        </div>
      </div>
    </div>
  )
}
