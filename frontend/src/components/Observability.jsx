import { useEffect, useState } from 'react'
import { api, fmt } from '../lib/api.js'

/**
 * System logs and metrics — the "check logs, metrics and system data" surface.
 * These are the same readings the agent's read_logs / read_metrics tools see.
 */
const KEY_METRICS = [
  ['gateway.comms_retries_per_min', 'gw retries/min', 10],
  ['gateway.forward_rate_hz', 'forward rate Hz', null],
  ['gateway.forward_latency_ms', 'forward age ms', 2000],
  ['plc.scan_time_ms', 'plc scan ms', 12],
  ['pt102.update_rate_hz', 'ref rate Hz', null],
  ['process.pressure_error_bar', 'control error bar', null],
]

const LEVEL_TONE = { ERROR: 'var(--bad)', WARN: 'var(--warn)', INFO: 'var(--ink-faint)' }

function Spark({ points }) {
  if (!points || points.length < 2) return null
  const vs = points.map((p) => p[1])
  const lo = Math.min(...vs), hi = Math.max(...vs)
  const span = hi - lo || 1
  const d = points.map((p, i) =>
    `${i ? 'L' : 'M'}${(i / (points.length - 1)) * 54},${14 - ((p[1] - lo) / span) * 12}`
  ).join('')
  return (
    <svg width="54" height="15" style={{ flex: 'none' }}>
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.2" />
    </svg>
  )
}

export default function Observability({ tick }) {
  const [metrics, setMetrics] = useState([])
  const [lines, setLines] = useState([])
  const [tab, setTab] = useState('metrics')

  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const [m, l] = await Promise.all([
          api.metrics(null, 60),
          api.logs({ level: 'INFO', limit: 40 }),
        ])
        if (!alive) return
        setMetrics(m.metrics || [])
        setLines((l.lines || []).slice().reverse())
      } catch { /* ignore */ }
    }
    poll()
    const h = setInterval(poll, 2000)
    return () => { alive = false; clearInterval(h) }
  }, [])

  const byName = Object.fromEntries(metrics.map((m) => [m.metric, m]))
  const warnCount = lines.filter((l) => l.level === 'WARN' || l.level === 'ERROR').length

  return (
    <div className="panel">
      <h3>System data
        <span className="right">
          <button className="ghost" style={{ padding: '2px 7px', fontSize: 10 }}
                  onClick={() => setTab(tab === 'metrics' ? 'logs' : 'metrics')}>
            {tab === 'metrics' ? `logs (${warnCount})` : 'metrics'}
          </button>
        </span>
      </h3>

      {tab === 'metrics' && KEY_METRICS.map(([name, label, warnAbove]) => {
        const m = byName[name]
        const v = m?.latest
        const hot = warnAbove != null && v != null && v > warnAbove
        return (
          <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 6,
                                   padding: '3px 0', borderBottom: '1px dotted var(--line)' }}>
            <span className="muted" style={{ flex: 1, fontSize: 10.5 }}>{label}</span>
            <Spark points={m?.points} />
            <span className="mono" style={{ fontSize: 11.5, minWidth: 46, textAlign: 'right',
                                            color: hot ? 'var(--bad)' : 'var(--ink)' }}>
              {v == null ? '—' : fmt(v, 1)}
            </span>
          </div>
        )
      })}

      {tab === 'logs' && (
        <div className="scroll" style={{ maxHeight: 220 }}>
          {lines.length === 0 && <div className="muted">No log lines yet.</div>}
          {lines.map((l, i) => (
            <div key={i} style={{ padding: '3px 0', borderBottom: '1px dotted var(--line)' }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'baseline' }}>
                <span className="mono" style={{ fontSize: 9.5, color: LEVEL_TONE[l.level] }}>
                  {l.level}
                </span>
                <span className="mono" style={{ fontSize: 9.5, color: 'var(--ink-faint)' }}>
                  {l.source}
                </span>
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--ink-dim)' }}>{l.message}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
