import { useState } from 'react'
import type { CostRecord, CostView } from '../types'

// AN UNMEASURED COST IS NOT A FREE ONE, and this screen exists to make that
// visible rather than to total a column. `Budget.spend` counts a call whose
// provider reported no cost as `unmeasured` rather than as zero, precisely so a
// ceiling enforced against a short number can say it is short. Rendering the
// total without that count would undo the accounting one layer up.

function money(value: number): string {
  return `$${value.toFixed(4)}`
}

function Record({ record }: { record: CostRecord }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="panel">
      <button className="finding-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="verdict">{money(record.spent_usd)}</span>
        <span className="sev">{record.lens}</span>
        <span className="subject">{record.run_id}</span>
        <span className="title">
          {record.calls} call{record.calls === 1 ? '' : 's'} &middot;{' '}
          {record.tokens.toLocaleString()} tokens
          {record.unmeasured_calls > 0 && (
            <> &middot; {record.unmeasured_calls} unmeasured</>
          )}
        </span>
      </button>
      {open && (
        <div className="finding-body">
          {record.stages.length === 0 ? (
            <p className="muted">
              This lens built a budget and made no billable call. That is a
              fact, not an absence &mdash; a lens that never got as far as a
              budget has no record here at all.
            </p>
          ) : (
            <table className="stages">
              <thead>
                <tr>
                  <th>stage</th>
                  <th>subject</th>
                  <th>cost</th>
                  <th>tokens</th>
                  <th>seconds</th>
                  <th>source</th>
                </tr>
              </thead>
              <tbody>
                {record.stages.map((stage, i) => (
                  <tr key={i}>
                    <td>{stage.stage}</td>
                    <td className="subject">{stage.subject}</td>
                    <td>
                      {/* null is UNMEASURED, not zero. */}
                      {stage.cost_usd === null ? (
                        <span className="unmeasured">unmeasured</span>
                      ) : (
                        money(stage.cost_usd)
                      )}
                    </td>
                    <td>{stage.tokens.toLocaleString()}</td>
                    <td>{stage.wall_seconds.toFixed(1)}</td>
                    <td className="muted">{stage.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </li>
  )
}

export default function Cost({ view }: { view: CostView }) {
  return (
    <>
      <div className="totals">
        <div>
          <span className="label">recorded spend</span>
          <strong>{money(view.total_usd)}</strong>
        </div>
        <div>
          <span className="label">model calls</span>
          <strong>{view.total_calls.toLocaleString()}</strong>
        </div>
        <div className={view.unmeasured_calls > 0 ? 'alarm' : ''}>
          <span className="label">unmeasured calls</span>
          <strong>{view.unmeasured_calls.toLocaleString()}</strong>
        </div>
      </div>

      {view.unmeasured_calls > 0 && (
        <div className="banner banner-warn">
          <strong>
            {view.unmeasured_calls} call
            {view.unmeasured_calls === 1 ? '' : 's'} reported no cost at all.
          </strong>{' '}
          Those are not free, they are unknown &mdash; so the total above is
          short by an unknown amount, and any ceiling was enforced against a
          number that was short too.
        </div>
      )}

      {view.unreadable.length > 0 && (
        <div className="banner banner-alarm">
          <strong>Spend that happened and cannot be shown.</strong>
          <ul>
            {view.unreadable.map((problem, i) => (
              <li key={i}>{problem}</li>
            ))}
          </ul>
        </div>
      )}

      {view.records.length === 0 ? (
        <p className="muted">
          No cost records. Nothing that makes model calls has run here yet
          &mdash; <code>hygiene</code> makes none and correctly records none.
        </p>
      ) : (
        <>
          <p className="sub">
            Accounted for: {view.lenses_with_records.join(', ')}. A lens a run
            reports having executed that is not in that list spent nothing
            because it never reached a model, and said so in a skip.
          </p>
          <ul className="costs">
            {view.records.map((record, i) => (
              <Record key={`${record.run_id}-${record.lens}-${i}`} record={record} />
            ))}
          </ul>
        </>
      )}
    </>
  )
}
