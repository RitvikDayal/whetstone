import { useState } from 'react'
import { ApiError, apiPost } from '../session'
import { DISPOSITIONS, type Disposition, type Finding, type RunView } from '../types'

// EVERY STRING RENDERED HERE IS UNTRUSTED. Finding titles, details, grade
// reasons and skip text are model output derived from the contents of a
// repository the user did not necessarily write. React escapes text children by
// default and that default is the whole defence: there is no raw-HTML rendering
// anywhere in this app, no markdown renderer, and a test greps for both.

function Banner({ run }: { run: RunView | null }) {
  // NO RUN AT ALL is not the same as a clean run, and rendering both as silence
  // is how a project nobody has checked reads as a project with no problems.
  if (run === null) {
    return (
      <div className="banner banner-quiet">
        No run has been recorded for this project. Nothing has been checked yet
        &mdash; start one from the <strong>Run</strong> tab.
      </div>
    )
  }
  return (
    <>
      {!run.finished && (
        <div className="banner banner-alarm">
          <strong>This run did not finish</strong> &mdash; status{' '}
          <code>{run.status}</code>. What follows is a partial record of a
          partial run: a finding that is absent may simply never have been
          looked for.
        </div>
      )}
      {run.skips.length > 0 && (
        <div className="banner banner-warn">
          <strong>Not everything was checked.</strong>
          <ul>
            {run.skips.map((skip, i) => (
              <li key={i}>{skip}</li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

function Verdict({ finding }: { finding: Finding }) {
  // THE WORD, not the letter. A grade D rendered as a bare "D" is a distinction
  // a skimming reader does not make, and a killed finding looking identical to
  // a confirmed one is exactly the defect `readmodel` carries `killed` for.
  if (!finding.graded) {
    return (
      <span className="verdict verdict-none" title="This lens does not grade.">
        not graded
      </span>
    )
  }
  if (finding.killed) {
    return <span className="verdict verdict-killed">D &mdash; killed</span>
  }
  return <span className={`verdict verdict-${finding.grade}`}>{finding.grade}</span>
}

// Which dispositions need which argument. NOT a copy of the rule -- the server
// enforces it and returns `dispositions.py`'s own sentence when it is missing.
// This only decides which input to show, and a wrong guess here produces a
// clear error from the authority rather than a silent divergence.
const NEEDS: Partial<Record<Disposition, { field: string; label: string }>> = {
  reject: { field: 'reason', label: 'Why (required)' },
  defer: { field: 'wake', label: 'Wake on (a date or a condition)' },
  hand_off: { field: 'assignee', label: 'Who takes it' },
}

function Decide({
  finding,
  token,
  onDecided,
}: {
  finding: Finding
  token: string
  onDecided: () => void
}) {
  const [disposition, setDisposition] = useState<Disposition>('verify')
  const [argument, setArgument] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const needs = NEEDS[disposition]

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const body: Record<string, unknown> = { disposition }
      if (needs) body[needs.field] = argument
      // MIRRORS THE CLI'S PROMPT. `reject` is the one decision no later run
      // undoes, and the CLI asks before applying it. A browser has no prompt,
      // so the confirmation is an explicit act here too -- the server refuses
      // without it rather than assuming.
      if (disposition === 'reject') {
        if (
          !window.confirm(
            'Rejecting is permanent. It suppresses this finding on every ' +
              'future run, and no later run undoes it. Continue?',
          )
        ) {
          setBusy(false)
          return
        }
        body.confirm = true
      }
      await apiPost(`api/findings/${finding.id}/decide`, token, body)
      onDecided()
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="decide">
      <label>
        Decision
        <select
          value={disposition}
          onChange={(e) => {
            setDisposition(e.target.value as Disposition)
            setArgument('')
            setError(null)
          }}
        >
          {DISPOSITIONS.map((d) => (
            <option key={d} value={d}>
              {d.replace('_', ' ')}
            </option>
          ))}
        </select>
      </label>
      {needs && (
        <label>
          {needs.label}
          <input
            value={argument}
            onChange={(e) => setArgument(e.target.value)}
            placeholder={needs.label}
          />
        </label>
      )}
      <button className="primary" disabled={busy} onClick={submit}>
        {busy ? 'Recording…' : 'Record decision'}
      </button>
      {error && <p className="error">{error}</p>}
    </div>
  )
}

function Row({
  finding,
  token,
  onDecided,
}: {
  finding: Finding
  token: string
  onDecided: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <li className={finding.killed ? 'finding killed' : 'finding'}>
      <button
        className="finding-head"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Verdict finding={finding} />
        <span className={`sev sev-${finding.severity}`}>{finding.severity}</span>
        <span className="subject">{finding.subject}</span>
        <span className="title">{finding.title}</span>
      </button>
      {open && (
        <div className="finding-body">
          <p className="detail">{finding.detail}</p>
          {finding.grade_reason && (
            <p className="why">
              <strong>Why this grade:</strong> {finding.grade_reason}
            </p>
          )}
          <dl className="meta">
            <dt>id</dt>
            <dd>
              <code>{finding.short_id}</code>
            </dd>
            <dt>lens</dt>
            <dd>{finding.lens}</dd>
            <dt>rule</dt>
            <dd>{finding.rule_id}</dd>
            <dt>state</dt>
            <dd>{finding.state}</dd>
          </dl>
          <Decide finding={finding} token={token} onDecided={onDecided} />
        </div>
      )}
    </li>
  )
}

export default function Findings({
  findings,
  run,
  token,
  onDecided,
}: {
  findings: Finding[]
  run: RunView | null
  token: string
  onDecided: () => void
}) {
  return (
    <>
      <p className="sub">
        {findings.length} open finding{findings.length === 1 ? '' : 's'}
        {run && (
          <>
            {' '}
            &middot; tier {run.tier} &middot; {run.file_count} file
            {run.file_count === 1 ? '' : 's'} in scope
          </>
        )}
      </p>

      <Banner run={run} />

      {findings.length === 0 ? (
        <p className="muted">
          {run && run.skips.length > 0
            ? 'No open findings — but see above: this run did not check everything.'
            : 'No open findings.'}
        </p>
      ) : (
        <ul className="findings">
          {/* NOT re-sorted. `store/findings.py` orders by grade first and puts
              an ABSENT grade between B and C, so a measured CVE is neither
              buried under something the falsifier refuted nor ranked above a
              proven crash. Sorting by severity here because that column looks
              more important would silently invert that. */}
          {findings.map((f) => (
            <Row key={f.id} finding={f} token={token} onDecided={onDecided} />
          ))}
        </ul>
      )}

      {findings.some((f) => f.killed) && (
        <p className="footnote">
          Rows marked <em>killed</em> were refuted by the falsifier. They are
          shown, and sorted last, because a tool that quietly drops what it
          refuted cannot be checked.
        </p>
      )}
    </>
  )
}
