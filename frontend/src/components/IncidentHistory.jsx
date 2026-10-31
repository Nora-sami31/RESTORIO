import { fmt } from '../lib/api.js'
import ForecastBanner from './ForecastBanner.jsx'

const TONE = {
  recovered: 'ok', recovered_under_containment: 'contain',
  needs_review: 'warn', safe_state_unresolved: 'warn',
}

/**
 * Past incidents from the audit trail. Clicking one opens its full record in
 * the inspector — what happened, what evidence was used, what changed, which
 * checks passed — which is the point of keeping an audit trail at all.
 */
export default function IncidentHistory({ rows, viewing, onOpen, onBackToLive,
                                          forecast, onRunForecast }) {
  if (!rows?.length) {
    return (
      <div className="panel">
        <h3>Incident history</h3>
        <ForecastBanner data={forecast} onRun={onRunForecast} />
        <div className="muted">No closed incidents yet. Each one is written to
          the audit trail and can be reopened here afterwards.</div>
      </div>
    )
  }
  return (
    <div className="panel">
      <h3>Incident history
        <span className="right muted">{rows.length} recorded</span>
      </h3>

      <ForecastBanner data={forecast} onRun={onRunForecast} />

      {viewing && (
        <button className="primary" style={{ width: '100%', marginBottom: 8 }}
                onClick={onBackToLive}>
          ← back to the live incident
        </button>
      )}

      <div className="scroll" style={{ maxHeight: 220 }}>
        {rows.map((r) => (
          <div key={r.incident_id}
               onClick={() => onOpen(r.incident_id)}
               className={`asset ${viewing === r.incident_id ? 'sel' : ''}`}
               style={{ display: 'block', cursor: 'pointer' }}>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <span className={`pill ${TONE[r.status] || ''}`} style={{ fontSize: 9.5 }}>
                {(r.status || 'open').replace(/_/g, ' ')}
              </span>
              <span className="muted mono" style={{ fontSize: 9.5, marginLeft: 'auto' }}>
                {new Date((r.created_at || 0) * 1000).toLocaleTimeString()}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 10.5, marginTop: 3 }}>
              {(r.diagnosis || 'no diagnosis recorded').slice(0, 96)}
            </div>
            <div className="muted mono" style={{ fontSize: 9.5, marginTop: 2 }}>
              {r.scenario_key} · {fmt(r.duration_s, 1)}s · {r.tool_calls} tools
              {r.provider ? ` · ${r.provider}` : ''}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
