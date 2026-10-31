import { useState } from 'react'

/**
 * The forward-looking half of the audit trail: what the recorded history says
 * is likely to go wrong next.
 *
 * The statistics underneath are deterministic, but the forecast itself is the
 * agent's — it chooses which history to read, decides which of the fault
 * classes matters, and writes the claim. The checks row is the deterministic
 * half: a forecast whose basis is not in the record is rejected before it gets
 * here, so what you are reading has already been tested against the data.
 *
 * Producing one costs a provider call, so it runs on request rather than on
 * every dashboard poll.
 */
export default function ForecastBanner({ data, onRun }) {
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)

  const hist = data?.history
  const agent = data?.agent
  const live = data?.live

  // The figures the record actually holds for each class the agent cited.
  // Rendered beside the agent's own wording so a number that does not match
  // the record is visible rather than taken on trust.
  const cited = (agent?.scenario_keys || [])
    .map((k) => (hist?.classes || []).find((c) => c.scenario_key === k))
    .filter(Boolean)

  const run = async () => {
    setBusy(true)
    try { await onRun() } finally { setBusy(false) }
  }

  // Nothing recorded yet — say so plainly rather than showing an empty frame.
  if (!hist?.available) {
    return (
      <div className="forecast">
        <div className="forecast-head">
          <span className="pill">forecast</span>
          <span className="muted" style={{ fontSize: 10.5 }}>
            no closed incidents to learn from yet
          </span>
        </div>
      </div>
    )
  }

  const span = hist.observed_span_h
  const n = hist.incidents
  const failed = (agent?.checks || []).filter((c) => !c.passed)

  return (
    <div className="forecast">
      <div className="forecast-head">
        <span className="pill">forecast</span>
        {agent?.available && (
          <span className={`pill ${agent.agentic ? 'contain' : ''}`}
                style={{ fontSize: 9 }}>
            {agent.agentic ? `agent · ${agent.provider}` : 'rules baseline'}
          </span>
        )}
        <span className="muted mono" style={{ fontSize: 9.5, marginLeft: 'auto' }}>
          {n} incidents · {span} h of demonstration history
        </span>
        <button className="ghost" style={{ fontSize: 9.5, padding: '2px 7px' }}
                disabled={busy} onClick={run}>
          {busy ? 'reading…' : agent ? 're-run' : 'forecast'}
        </button>
      </div>

      {!agent && (
        <div className="muted" style={{ fontSize: 10.5, marginTop: 5 }}>
          {hist.headline
            ? `${hist.headline.scenario_key.replace(/_/g, ' ')} has recurred
               ${hist.headline.n}× and escalated ${hist.headline.escalated} of
               those. Run the forecast to have the agent read the history and
               say what it expects next.`
            : `No fault class has reached ${hist.headline_min_n} occurrences yet —
               too thin to forecast from.`}
        </div>
      )}

      {agent && !agent.available && (
        <div className="muted" style={{ fontSize: 10.5, marginTop: 5 }}>
          No forecast recorded: {agent.reason || 'rejected by the claim check'}.
          {failed.length > 0 && (
            <div className="mono" style={{ fontSize: 9.5, marginTop: 3 }}>
              {failed.map((c) => `✗ ${c.name}`).join(' · ')}
            </div>
          )}
        </div>
      )}

      {agent?.available && (
        <>
          <div className="forecast-claim">{agent.claim}</div>

          <div className="forecast-meta mono">
            <span>{agent.asset}</span>
            <span>·</span>
            <span>{agent.horizon_h} h horizon</span>
            <span>·</span>
            <span>confidence {agent.confidence}</span>
            <span>·</span>
            <span className={failed.length ? 'bad' : 'ok'}>
              {agent.checks?.length - failed.length}/{agent.checks?.length} structural checks
            </span>
          </div>

          {agent.watch?.length > 0 && (
            <ul className="forecast-watch">
              {agent.watch.slice(0, 3).map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          )}

          <button className="ghost forecast-more" onClick={() => setOpen(!open)}>
            {open ? 'hide the basis' : 'why it says this'}
          </button>

          {open && (
            <div className="forecast-basis">
              <div><b>Mechanism.</b> {agent.mechanism}</div>
              <div style={{ marginTop: 4 }}><b>Basis.</b> {agent.basis}</div>

              {cited.length > 0 && (
                <div style={{ marginTop: 5 }}>
                  <b>What the record holds.</b>
                  <ul className="forecast-watch">
                    {cited.map((c) => (
                      <li key={c.scenario_key} className="mono">
                        {c.scenario_key}: n={c.n}, {c.escalated} escalated
                        {c.mean_interval_s
                          ? `, mean gap ${(c.mean_interval_s / 3600).toFixed(2)} h`
                          : ', no interval'}
                        {c.interval_cv != null ? ` (spread ${c.interval_cv})` : ''}
                        {c.interval_model_holds ? '' : ' — not periodic'}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {live?.projectable && (
                <div style={{ marginTop: 4 }}>
                  <b>Live trend.</b> {live.detail}
                </div>
              )}

              {agent.tool_trail?.length > 0 && (
                <div style={{ marginTop: 5 }}>
                  <b>What it read.</b>
                  <ul className="forecast-watch">
                    {agent.tool_trail.map((t, i) => (
                      <li key={i} className="mono">
                        {t.tool}{t.summary ? ` → ${t.summary}` : ''}
                        {t.accepted === false ? ' → rejected, revised' : ''}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div style={{ marginTop: 5 }}>
                <b>Checks.</b>
                <ul className="forecast-watch">
                  {(agent.checks || []).map((c, i) => (
                    <li key={i} className={c.passed ? '' : 'bad'}>
                      {c.passed ? '✓' : '✗'} {c.detail}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="muted" style={{ marginTop: 5, fontSize: 9.5 }}>
                The checks test structure, not wording: that every cited class
                exists and is thick enough, that the asset appears in the
                record, that the horizon fits it, and that any claim about
                timing rests on a class actually recurring on an interval.
                They do not verify the prose above — compare it against the
                recorded figures.
              </div>
              {hist?.provenance_note && (
                <div className="muted" style={{ marginTop: 4, fontSize: 9.5 }}>
                  {hist.provenance_note}
                </div>
              )}
              <div className="muted" style={{ marginTop: 4, fontSize: 9.5 }}>
                Advisory only. A forecast is not evidence for an intervention —
                the agent cannot cite it to justify changing the plant.
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
