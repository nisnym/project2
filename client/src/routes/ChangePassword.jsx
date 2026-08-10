import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Page } from '../components/AppShell'
import { Banner, Button, ErrorBanner, Field, Panel, TextInput, useToast } from '../components/kit'
import { api } from '../lib/api'
import { useAuth } from '../lib/authContext'

/**
 * Change your own password.
 *
 * Shown as a wall — not a dismissible prompt — when an administrator has reset
 * the password or minted the account with a generated one. The temporary
 * credential travelled out of band to get here, so the window in which it is
 * useful to anyone who saw it in transit should be as short as the person can
 * make it.
 */
export default function ChangePassword() {
  const { user, signOut } = useAuth()
  const toast = useToast()
  const [form, setForm] = useState({ current_password: '', new_password: '', confirm: '' })

  const forced = Boolean(user?.must_change_password)
  const mismatch = Boolean(form.confirm) && form.new_password !== form.confirm
  const tooShort = Boolean(form.new_password) && form.new_password.length < 8
  const reused = Boolean(form.new_password) && form.new_password === form.current_password

  const save = useMutation({
    mutationFn: () =>
      api.post('/api/auth/change-password', {
        current_password: form.current_password,
        new_password: form.new_password,
      }),
    onSuccess: async () => {
      toast.ok('Password changed — signing you back in')
      // Every session was revoked server-side, including this one. Signing out
      // is the honest reflection of that rather than waiting for the next call
      // to fail with a 401.
      await signOut()
    },
  })

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  return (
    <Page
      title="Change your password"
      subtitle={forced ? 'Required before you can carry on.' : 'Choose something new.'}
    >
      <div style={{ maxWidth: 520 }}>
        <Panel tone="raised" ticks>
          <form
            className="stack"
            onSubmit={(event) => {
              event.preventDefault()
              save.mutate()
            }}
          >
            {forced && (
              <Banner tone="warn" title="Temporary password in use">
                This password was issued by an administrator. Replace it now — and
                if you were not expecting it, contact us before you do.
              </Banner>
            )}

            <Field label="Current password">
              {(id) => (
                <TextInput
                  id={id}
                  type="password"
                  required
                  autoComplete="current-password"
                  value={form.current_password}
                  onChange={set('current_password')}
                />
              )}
            </Field>

            <Field
              label="New password"
              hint="At least 8 characters"
              error={tooShort ? 'Too short.' : reused ? 'That is your current password.' : null}
            >
              {(id) => (
                <TextInput
                  id={id}
                  type="password"
                  required
                  autoComplete="new-password"
                  value={form.new_password}
                  invalid={tooShort || reused}
                  onChange={set('new_password')}
                />
              )}
            </Field>

            <Field label="Confirm new password" error={mismatch ? 'These do not match.' : null}>
              {(id) => (
                <TextInput
                  id={id}
                  type="password"
                  required
                  autoComplete="new-password"
                  value={form.confirm}
                  invalid={mismatch}
                  onChange={set('confirm')}
                />
              )}
            </Field>

            <ErrorBanner error={save.error} title="Could not change your password" />

            <Button
              type="submit"
              variant="primary"
              busy={save.isPending}
              disabled={
                !form.current_password || !form.new_password || mismatch || tooShort || reused
              }
            >
              Change password
            </Button>

            <p className="tiny faint" style={{ margin: 0 }}>
              Every other device will be signed out. If someone else knew the old
              password, leaving their session alive would make this change
              cosmetic.
            </p>
          </form>
        </Panel>
      </div>
    </Page>
  )
}
