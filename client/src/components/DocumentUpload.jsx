import { useState } from 'react'
import { Stamp } from './kit'

/**
 * Document capture for KYC.
 *
 * The file is hashed **in the browser** and only the digest is sent. That is
 * not a shortcut — it is the correct shape for this system: kyc-svc scores a
 * document by its hash, so shipping the bytes would put a passport scan in a
 * database that has no business holding one. The customer's document never
 * leaves their device.
 *
 * A real deployment would additionally upload the bytes to a dedicated document
 * vault under a separate credential; the hash sent here would still be what
 * kyc-svc stores.
 */

const DOC_TYPES = [
  { value: 'PASSPORT', label: 'Passport', hint: 'Photo page' },
  { value: 'NATIONAL_ID', label: 'National ID', hint: 'Both sides in one file' },
  { value: 'UTILITY_BILL', label: 'Proof of address', hint: 'Issued in the last 3 months' },
  { value: 'SELFIE', label: 'Selfie', hint: 'Face clearly visible' },
]

const MAX_BYTES = 10 * 1024 * 1024

async function sha256Hex(file) {
  const buffer = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

export default function DocumentUpload({ documents, onChange }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  async function accept(docType, file) {
    if (!file) return
    setError(null)
    if (file.size > MAX_BYTES) {
      setError(`${file.name} is larger than 10 MB.`)
      return
    }
    setBusy(docType)
    try {
      const sha256 = await sha256Hex(file)
      onChange([
        ...documents.filter((doc) => doc.doc_type !== docType),
        { doc_type: docType, sha256, filename: file.name },
      ])
    } catch {
      setError('Could not read that file.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div>
      <div className="field__label">
        <span className="label label--ink">Documents</span>
        <span className="field__hint">{documents.length} of 4 attached</span>
      </div>

      <div
        style={{
          display: 'grid',
          gap: 'var(--hair)',
          background: 'var(--rule-strong)',
          border: '2px solid var(--frame)',
        }}
      >
        {DOC_TYPES.map((type) => {
          const attached = documents.find((doc) => doc.doc_type === type.value)
          return (
            <label
              key={type.value}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 'var(--s-3)',
                padding: 'var(--s-3) var(--s-4)',
                background: attached ? 'var(--allow-wash)' : 'var(--surface-raised)',
                cursor: 'pointer',
              }}
            >
              <span style={{ minWidth: 0 }}>
                <span style={{ fontWeight: 600, fontSize: 'var(--step--1)' }}>
                  {type.label}
                </span>
                <span className="tiny faint" style={{ display: 'block' }}>
                  {attached ? (
                    <span className="mono">{attached.filename}</span>
                  ) : (
                    type.hint
                  )}
                </span>
              </span>

              <span style={{ flex: 'none' }}>
                {busy === type.value ? (
                  <Stamp tone="live" working>
                    Hashing
                  </Stamp>
                ) : attached ? (
                  <Stamp tone="allow">Attached</Stamp>
                ) : (
                  <Stamp tone="pending">Add</Stamp>
                )}
              </span>

              <input
                type="file"
                className="sr-only"
                accept="image/*,application/pdf"
                onChange={(event) => accept(type.value, event.target.files?.[0])}
              />
            </label>
          )
        })}
      </div>

      {error && (
        <p className="field__error" role="alert">
          {error}
        </p>
      )}

      <p className="tiny faint" style={{ marginTop: 'var(--s-2)' }}>
        Files are fingerprinted on your device. Only the fingerprint is sent to us —
        the document itself never leaves your computer.
      </p>
    </div>
  )
}
