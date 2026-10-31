import { api } from '../lib/api.js'

const STATE_TONE = {
  recovered: 'ok', recovered_under_containment: 'contain',
  needs_review: 'warn', safe_state_unresolved: 'warn',
  detected: 'bad', investigating: 'warn', response_proposed: 'warn',
  validating: 'warn', applying: 'contain', verifying: 'warn',
  monitoring: 'contain',
}

const STANCE = { supported: 'ok', contradicted: 'bad', unresolved: 'warn' }

/** What happened, what evidence was used, what changed, what was verified. */
export default function IncidentInspector({ incident, historic, onRefresh }) {
  if (!incident) {
    return <div className="panel"><h3>Incident inspector</h3>
      <div className="muted">No incident. Load a scenario to begin.</div></div>
  }

  const ver = incident.verifications?.[incident.verifications.length - 1]

  return (
    <div className="panel">
      <h3>Incident inspector
        <span className="right">
          <span className={`pill ${STATE_TONE[incident.state] || ''}`}>
            <span className="dot" />{incident.state.replace(/_/g, ' ')}
          </span>
        </span>
      </h3>

      <div className="muted mono" style={{ marginBottom: 8 }}>
        {incident.incident_id}
        {historic && <span className="pill" style={{ marginLeft: 8, fontSize: 9.5 }}>
          from the audit trail</span>}
      </div>

      {!historic && <ApprovalBanner incident={incident} onRefresh={onRefresh} />}

      {incident.detected_reasons?.length > 0 && (
        <>
          <div className="muted">
            Why it triggered <span style={{ opacity: .75 }}>— readings at the
            moment of detection, not current</span>
          </div>
          {incident.detected_reasons.map((r, i) => (
            <div key={i} style={{ fontSize: 12, marginBottom: 4 }}>{r}</div>
          ))}
          <div className="divider" />
        </>
      )}

      {incident.diagnosis && (
        <>
          <div className="muted">
            Diagnosis <span style={{ opacity: .75 }}>— what was wrong, and what
            was done about it</span>
          </div>
          <div style={{ fontSize: 13, marginBottom: 6 }}>{incident.diagnosis}</div>
          <div className="kv"><span className="k">Stated confidence</span>
            <span className="v">{(incident.confidence * 100).toFixed(0)} %</span></div>
          {incident.operator_note && (
            <div className="banner info" style={{ marginTop: 8 }}>
              <b>For the operator:</b> {incident.operator_note}
            </div>
          )}
          <div className="divider" />
        </>
      )}

      {incident.hypotheses?.length > 0 && (
        <>
          <div className="muted" style={{ marginBottom: 6 }}>Explanations compared</div>
          {incident.hypotheses.map((h, i) => (
            <div key={i} style={{ marginBottom: 7 }}>
              <span className={`pill ${STANCE[h.stance] || ''}`}>{h.stance}</span>{' '}
              <b style={{ fontSize: 12 }}>{h.name}</b>
              <div className="muted" style={{ marginTop: 2 }}>{h.rationale}</div>
              {h.evidence_ids?.length > 0 && (
                <div className="muted mono" style={{ fontSize: 10 }}>
                  evidence: {h.evidence_ids.join(', ')}
                </div>
              )}
            </div>
          ))}
          <div className="divider" />
        </>
      )}

      {incident.executions?.length > 0 && (
        <>
          <div className="muted" style={{ marginBottom: 6 }}>What changed</div>
          {incident.executions.map((e, i) => (
            <div key={i} className="kv">
              <span className="k mono">{e.outcome}</span>
              <span className="v" style={{ maxWidth: 250, textAlign: 'right', fontSize: 11 }}>
                {e.detail}<br />
                <span className="muted">rev {e.prior_revision} → {e.new_revision}</span>
              </span>
            </div>
          ))}
          <div className="divider" />
        </>
      )}

      {incident.state === 'monitoring' && (
        <div className="banner info">
          <b>Monitoring</b> — the checks passed; the station is being watched to
          confirm the recovery holds before the incident closes.
        </div>
      )}

      {incident.monitoring?.length > 0 && (
        <>
          <div className="muted" style={{ marginBottom: 6 }}>
            Post-recovery monitoring{' '}
            {incident.monitoring[incident.monitoring.length - 1].passed
              ? '(held)' : '(did NOT hold)'}
          </div>
          {incident.monitoring[incident.monitoring.length - 1].checks.map((c, i) => (
            <div className={`check ${c.passed ? 'pass' : 'fail'}`} key={i}>
              <span className="mk">{c.passed ? '✓' : '✗'}</span>
              <span><span className="nm">{c.name}</span> <span className="de">{c.detail}</span></span>
            </div>
          ))}
          <div className="divider" />
        </>
      )}

      {ver && (
        <>
          <div className="muted" style={{ marginBottom: 6 }}>
            Independent verification {ver.passed ? '(passed)' : '(failed)'}
          </div>
          {ver.checks.map((c, i) => (
            <div className={`check ${c.informational ? 'info' : (c.passed ? 'pass' : 'fail')}`} key={i}>
              <span className="mk">{c.informational ? '·' : (c.passed ? '✓' : '✗')}</span>
              <span><span className="nm">{c.name}</span> <span className="de">{c.detail}</span></span>
            </div>
          ))}
          <div className="divider" />
        </>
      )}

      {incident.cited_evidence?.length > 0 && (
        <div className="muted mono" style={{ fontSize: 10 }}>
          cited evidence: {incident.cited_evidence.join(', ')}
        </div>
      )}
      <div className="muted" style={{ marginTop: 6 }}>
        {incident.duration_s?.toFixed(1)} s · {incident.tool_calls ?? '—'} tool calls
        {incident.provider ? ` · ${incident.provider}` : ''}
      </div>
    </div>
  )
}


/**
 * The operator's approve/decline control.
 *
 * It lives inside the inspector normally, and is rendered again as a pinned
 * card whenever the inspector is collapsed or the station view is maximised.
 * An approval pause stops the agent, so the control must never be somewhere
 * the operator has to go looking for it.
 */
export function ApprovalBanner({ incident, onRefresh, pinned = false }) {
  const pending = incident?.pending_approval
  if (!pending) return null

  const act = async (go) => {
    try {
      if (go) await api.approve(incident.incident_id, pending.proposal_id)
      else await api.deny(incident.incident_id, pending.proposal_id, 'Declined by operator.')
      onRefresh?.()
    } catch (e) { console.error(e) }
  }

  return (
    <div className={`banner warn${pinned ? ' approval-pinned' : ''}`}>
      <b>Approval required</b> — the agent proposes <code>{pending.action}</code>{' '}
      <code>{JSON.stringify(pending.changes)}</code> (risk: {pending.risk}).
      <div style={{ marginTop: 4 }}>{pending.rationale}</div>
      <div className="row" style={{ marginTop: 8 }}>
        <button className="primary" onClick={() => act(true)}>Approve &amp; apply</button>
        <button className="danger" onClick={() => act(false)}>Decline</button>
      </div>
    </div>
  )
}
