import { useEffect, useRef, useState } from 'react'
import { ApiError, apiGet, apiPost, streamEvents } from '../session'
import type { ConfigView, RunEvent } from '../types'

// THE CEILING IS SHOWN WITH WHAT IT DOES NOT BOUND. `usd_per_run` is enforced
// per LENS (issue #43), `calls_per_day` is accepted and not enforced at all,
// and nothing bounds how many runs are started. A CLI user typing `whetstone
// run` has friction that limits the last one in practice; a button does not.
// The server returns those caveats as data so this screen cannot render the
// number without them.

function Ceiling({ config }: { config: ConfigView }) {
  return (
    <section className="panel">
      <h2>What this run is held to</h2>
      <dl className="meta wide">
        <dt>project</dt>
        <dd>{config.project}</dd>
        <dt>tier</dt>
        <dd>{config.tier}</dd>
        <dt>ceiling</dt>
        <dd>
          {config.usd_per_run === null
            ? 'none set — this run is unbounded in dollars'
            : `$${config.usd_per_run.toFixed(4)} per lens`}
        </dd>
        <dt>lenses</dt>
        <dd>{config.lenses.length ? config.lenses.join(', ') : 'none declared'}</dd>
      </dl>
      {config.caveats.length > 0 && (
        <div className="banner banner-warn">
          <strong>What that ceiling does not bound.</strong>
          <ul>
            {config.caveats.map((caveat, i) => (
              <li key={i}>{caveat}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function Progress({ events }: { events: RunEvent[] }) {
  if (events.length === 0) return null

  const started = events.find((e) => e.kind === 'run_started')
  const finished = events.find((e) => e.kind === 'run_finished')
  const failed = events.find((e) => e.kind === 'error')
  const running = new Set<string>()
  for (const event of events) {
    if (event.kind === 'lens_started') running.add(event.lens)
    if (event.kind === 'lens_finished') running.delete(event.lens)
  }

  return (
    <section className="panel">
      <h2>
        {failed
          ? 'The run failed'
          : finished
            ? `Run ${finished.status}`
            : 'Running…'}
      </h2>
      {started && started.kind === 'run_started' && (
        <p className="muted">
          <code>{started.run_id}</code> &middot; tier {started.tier} &middot;{' '}
          {started.file_count} file{started.file_count === 1 ? '' : 's'} in scope
          &middot; {started.lens_count} lens
          {started.lens_count === 1 ? '' : 'es'}
        </p>
      )}
      <ol className="events">
        {events.map((event, i) => {
          if (event.kind === 'lens_started') {
            return (
              <li key={i} className={running.has(event.lens) ? 'live' : ''}>
                <strong>{event.lens}</strong> started
              </li>
            )
          }
          if (event.kind === 'lens_finished') {
            return (
              <li key={i}>
                <strong>{event.lens}</strong> finished &mdash; {event.new} new,{' '}
                {event.seen} already known
                {event.skips.length > 0 && (
                  <ul className="skips">
                    {event.skips.map((skip, j) => (
                      <li key={j}>{skip}</li>
                    ))}
                  </ul>
                )}
              </li>
            )
          }
          if (event.kind === 'run_finished') {
            return (
              <li key={i}>
                Run <strong>{event.status}</strong> &mdash; {event.new} new,{' '}
                {event.seen} already known
              </li>
            )
          }
          if (event.kind === 'error') {
            return (
              <li key={i} className="error">
                {event.error}
              </li>
            )
          }
          return null
        })}
      </ol>
      {(finished || failed) && (
        <p className="footnote">
          This list is a convenience. The record is the store &mdash; reload and
          the <strong>Findings</strong> tab shows the same run.
        </p>
      )}
    </section>
  )
}

export default function Run({
  token,
  onFinished,
}: {
  token: string
  onFinished: () => void
}) {
  const [config, setConfig] = useState<ConfigView | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const abort = useRef<AbortController | null>(null)

  useEffect(() => {
    apiGet<ConfigView>('api/config', token)
      .then(setConfig)
      .catch((exc: unknown) =>
        setError(exc instanceof ApiError ? exc.message : String(exc)),
      )
    return () => abort.current?.abort()
  }, [token])

  async function start() {
    setBusy(true)
    setError(null)
    setEvents([])
    try {
      const { ticket } = await apiPost<{ ticket: string }>('api/runs', token, {})
      const controller = new AbortController()
      abort.current = controller
      await streamEvents(
        `api/runs/${ticket}/events`,
        token,
        (event) => setEvents((prior) => [...prior, event as RunEvent]),
        controller.signal,
      )
      // The stream ended, which means the run reached a terminal event. Refresh
      // the queue from the STORE rather than assembling it from what streamed.
      onFinished()
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {config && <Ceiling config={config} />}

      <p>
        <button className="primary big" disabled={busy} onClick={start}>
          {busy ? 'Running…' : 'Start a run'}
        </button>{' '}
        <span className="muted">
          One run at a time per project, enforced across processes &mdash; a run
          started in a terminal blocks this button, and this button blocks that
          terminal.
        </span>
      </p>

      {error && <div className="banner banner-alarm">{error}</div>}

      <Progress events={events} />
    </>
  )
}
