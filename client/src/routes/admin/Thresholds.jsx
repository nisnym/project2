import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  ErrorBanner,
  Field,
  KeyValue,
  Loading,
  Panel,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/format'

/**
 * The two numbers that decide what happens to every payment.
 *
 * Drawn as a single 0–100 bar with the two cut points on it, because the thing
 * an administrator actually needs to see is how *wide the review band is* —
 * that is the analyst workload, and it is invisible if you only show two
 * numbers in two inputs.
 */

function BandBar({ allowBelow, blockAt }) {
  const allow = Math.max(0, Math.min(100, allowBelow))
  const block = Math.max(allow, Math.min(100, blockAt))

  return (
    <div>
      <div style={{ display: 'flex', height: 34, border: '2px solid var(--frame)' }}>
        <div
          style={{
            width: `${allow}%`,
            background: 'var(--allow)',
            display: 'grid',
            placeItems: 'center',
          }}
        >
          {allow > 12 && (
            <span className="label" style={{ color: 'var(--ink-inverse)' }}>
              Allow
            </span>
          )}
        </div>
        <div
          style={{
            width: `${block - allow}%`,
            background: 'var(--review)',
            display: 'grid',
            placeItems: 'center',
          }}
        >
          {block - allow > 12 && (
            <span className="label" style={{ color: 'var(--ink-inverse)' }}>
              Review
            </span>
          )}
        </div>
        <div
          style={{
            width: `${100 - block}%`,
            background: 'var(--block)',
            display: 'grid',
            placeItems: 'center',
          }}
        >
          {100 - block > 12 && (
            <span className="label" style={{ color: 'var(--ink-inverse)' }}>
              Block
            </span>
          )}
        </div>
      </div>
      <div className="row row--between" style={{ marginTop: 6 }}>
        <span className="mono tiny">0</span>
        <span className="mono tiny">{allow}</span>
        <span className="mono tiny">{block}</span>
        <span className="mono tiny">100</span>
      </div>
    </div>
  )
}

/**
 * Seeded from the server's current values on mount.
 *
 * The parent gives this a `key` of the threshold version, so a save (or another
 * administrator's change) remounts it with fresh values rather than syncing
 * server data into form state through an effect.
 */
function ThresholdForm({ current }) {
  const queryClient = useQueryClient()
  const toast = useToast()

  const [allowBelow, setAllowBelow] = useState(String(current.allow_below))
  const [blockAt, setBlockAt] = useState(String(current.block_at_or_above))

  const save = useMutation({
    mutationFn: () =>
      api.patch('/api/fraud/thresholds', {
        allow_below: Number(allowBelow),
        block_at_or_above: Number(blockAt),
      }),
    onSuccess: (updated) => {
      queryClient.invalidateQueries({ queryKey: ['fraud'] })
      toast.ok(`Thresholds saved as version ${updated.version}`)
    },
  })

  const allow = Number(allowBelow)
  const block = Number(blockAt)
  const invalid = !(allow >= 0 && allow <= block && block <= 100)
  const reviewWidth = block - allow

  return (
    <div className="split">
        <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
          <Panel title="Bands" tone="raised" ticks className="reveal">
            <BandBar allowBelow={allow} blockAt={block} />

            <div className="grid-2" style={{ marginTop: 'var(--s-5)' }}>
              <Field label="Allow below" hint="score < this passes">
                {(id) => (
                  <TextInput
                    id={id}
                    mono
                    inputMode="numeric"
                    value={allowBelow}
                    invalid={invalid}
                    onChange={(event) => setAllowBelow(event.target.value)}
                  />
                )}
              </Field>
              <Field label="Block at or above" hint="score ≥ this is declined">
                {(id) => (
                  <TextInput
                    id={id}
                    mono
                    inputMode="numeric"
                    value={blockAt}
                    invalid={invalid}
                    onChange={(event) => setBlockAt(event.target.value)}
                  />
                )}
              </Field>
            </div>

            {invalid && (
              <Banner tone="error" title="Impossible bands">
                Require 0 ≤ allow below ≤ block at ≤ 100.
              </Banner>
            )}

            {!invalid && reviewWidth <= 5 && (
              <Banner tone="warn" title="Very narrow review band">
                Only {reviewWidth} points separate allowing from blocking. Almost
                nothing will reach an analyst — borderline transfers will be declined
                outright instead.
              </Banner>
            )}

            {!invalid && reviewWidth >= 50 && (
              <Banner tone="warn" title="Very wide review band">
                {reviewWidth} points of score land in manual review. Check your
                analysts can absorb that volume before saving.
              </Banner>
            )}

            <ErrorBanner error={save.error} title="Could not save" />

            <div style={{ marginTop: 'var(--s-4)' }}>
              <Button
                variant="primary"
                busy={save.isPending}
                disabled={invalid}
                onClick={() => save.mutate()}
              >
                Save thresholds
              </Button>
            </div>
          </Panel>
        </div>

        <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
          <Panel title="In force" className="reveal">
            <KeyValue
              rows={[
                ['Allow', current.bands.allow],
                ['Review', current.bands.review],
                ['Block', current.bands.block],
                ['Version', <span key="v" className="mono">v{current.version}</span>],
                ['Updated', formatDateTime(current.updated_at)],
              ]}
            />
          </Panel>

          <Panel title="Safe harbour" className="reveal">
            <p className="small muted">
              Currently{' '}
              <strong className="mono">
                {current.safe_harbour_amount === '0.0000'
                  ? 'disabled'
                  : `₹${current.safe_harbour_amount}`}
              </strong>
              .
            </p>
            <Banner tone="info" title="Why this is off by default">
              Safe harbour auto-allows small payments when the fraud service is
              unreachable. Our standing decision is the opposite — an outage fails
              to review, never to allow. Turning this on is an explicit, recorded
              acceptance of risk.
            </Banner>
          </Panel>
        </div>
    </div>
  )
}

export default function Thresholds() {
  const thresholds = useQuery({
    queryKey: ['fraud', 'thresholds'],
    queryFn: () => api.get('/api/fraud/thresholds'),
  })

  return (
    <Page
      title="Score thresholds"
      subtitle="Where the line falls between allowing a payment, reviewing it, and blocking it."
    >
      {thresholds.isLoading ? (
        <Loading rows={5} />
      ) : thresholds.error ? (
        <ErrorBanner error={thresholds.error} title="Could not load the thresholds" />
      ) : (
        <ThresholdForm current={thresholds.data} key={thresholds.data.version} />
      )}
    </Page>
  )
}
