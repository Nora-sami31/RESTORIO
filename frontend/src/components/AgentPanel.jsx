import { useEffect, useRef } from 'react'

const CLS = {
  tool_call: 'tool', agent_message: 'say', test_report: 'test',
  execution: 'exec', verification: 'verify', incident_closed: 'concl',
  approval_requested: 'test', agent_escalation: 'fail', agent_model_error: 'fail',
  incident_detected: 'fail', decoy_probe: 'exec',
}

function Check({ c }) {
  const info = c.informational
  return (
    <div className={`check ${info ? 'info' : (c.passed ? 'pass' : 'fail')}`}>
      <span className="mk">{info ? '·' : (c.passed ? '✓' : '✗')}</span>
      <span><span className="nm">{c.name}</span> <span className="de">{c.detail}</span></span>
    </div>
  )
}

/** Evidence requested, diagnosis, candidates tested, actions executed. */
export default function AgentPanel({ events, incident }) {
  const box = useRef(null)
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight }, [events])

  const rows = events.filter((e) => CLS[e.kind])

  return (
    <div className="panel" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <h3>Agent activity
        {incident?.usage?.llm_calls > 0 && (
          <span className="right muted">
            {incident.usage.llm_calls} model calls ·{' '}
            {incident.usage.input_tokens + incident.usage.output_tokens} tokens
          </span>
        )}
      </h3>
      <div className="feed scroll" ref={box} style={{ flex: 1, minHeight: 120 }}>
        {rows.length === 0 && <div className="muted">No agent activity yet.</div>}
        {rows.map((e, i) => {
          const p = e.payload || {}
          const ts = new Date(e.ts * 1000).toLocaleTimeString()
          let name = e.kind, detail = null, extra = null

          if (e.kind === 'tool_call') {
            name = p.tool
            detail = <code>{JSON.stringify(p.arguments)}</code>
            extra = <div className="d">{String(p.result_preview || '').slice(0, 230)}</div>
          } else if (e.kind === 'agent_message') {
            name = 'reasoning'; detail = p.text
          } else if (e.kind === 'test_report') {
            name = p.passed ? 'candidate test passed' : 'candidate test FAILED'
            detail = p.summary
            extra = <div>{(p.checks || []).map((c, n) => <Check c={c} key={n} />)}</div>
          } else if (e.kind === 'execution') {
            name = `execution ${p.outcome}`
            detail = `${p.detail || ''} (revision ${p.prior_revision} → ${p.new_revision})`
          } else if (e.kind === 'verification') {
            name = p.passed ? 'VERIFIED RECOVERED' : 'verification FAILED'
            detail = p.summary
            extra = <div>{(p.checks || []).map((c, n) => <Check c={c} key={n} />)}</div>
          } else if (e.kind === 'incident_closed') {
            name = `closed: ${p.status}`; detail = p.diagnosis
          } else if (e.kind === 'incident_detected') {
            name = 'incident detected'; detail = (p.reasons || []).join(' ')
          } else if (e.kind === 'approval_requested') {
            name = 'approval requested'; detail = `${p.action} ${JSON.stringify(p.changes)}`
          } else if (e.kind === 'agent_escalation') {
            name = 'escalated'; detail = p.reason
          } else if (e.kind === 'agent_model_error') {
            name = 'model error'; detail = p.error
          } else if (e.kind === 'decoy_probe') {
            name = 'test-client write'
            detail = `routed to ${p.route}; operational state changed: ${p.operational_state_changed}`
          }

          const cls = (e.kind === 'test_report' && !p.passed) ||
                      (e.kind === 'verification' && !p.passed) ? 'fail' : CLS[e.kind]
          return (
            <div className={`ev ${cls}`} key={e.id || i}>
              <div className="h"><span className="t">{ts}</span><span className="n">{name}</span></div>
              {detail && <div className="d">{detail}</div>}
              {extra}
            </div>
          )
        })}
      </div>
    </div>
  )
}
