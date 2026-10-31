const J = async (r) => {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json()
}

export const api = {
  health:      ()            => fetch('/api/health').then(J),
  state:       ()            => fetch('/api/state').then(J),
  truth:       ()            => fetch('/api/truth').then(J),
  history:     ()            => fetch('/api/history').then(J),
  scenarios:   ()            => fetch('/api/scenarios').then(J),
  provider:    ()            => fetch('/api/provider').then(J),
  gateway:     ()            => fetch('/api/gateway').then(J),
  metrics:     (name, w = 60) => fetch(`/api/metrics?window_s=${w}${name ? `&name=${name}` : ''}`).then(J),
  logs:        (o = {})       => fetch('/api/logs?' + new URLSearchParams(
                                   Object.entries(o).filter(([, v]) => v != null))).then(J),
  decoy:       ()            => fetch('/api/decoy').then(J),
  accessLog:   ()            => fetch('/api/access-log').then(J),
  incidents:   ()            => fetch('/api/incidents').then(J),
  forecast:    ()            => fetch('/api/forecast').then(J),
  runForecast: ()            => fetch('/api/forecast/run', { method: 'POST' }).then(J),
  incident:    (id)          => fetch(`/api/incident/${id}`).then(J),
  asset:       (id)          => fetch(`/api/asset/${id}`).then(J),
  evidence:    (q = '')      => fetch(`/api/evidence?query=${encodeURIComponent(q)}`).then(J),
  loadScenario:(k, seed)     => fetch(`/api/scenario/${k}${seed ? `?seed=${seed}` : ''}`, { method: 'POST' }).then(J),
  sim:         (a, speed)    => fetch(`/api/sim/${a}${speed ? `?speed=${speed}` : ''}`, { method: 'POST' }).then(J),
  autonomy:    (m)           => fetch(`/api/autonomy/${m}`, { method: 'POST' }).then(J),
  runAgent:    ()            => fetch('/api/agent/run', { method: 'POST' }).then(J),
  generate:    (o = {})       => fetch('/api/generate?' + new URLSearchParams(
                                   Object.entries(o).filter(([, v]) => v != null)), { method: 'POST' }).then(J),
  approve:     (id, pid)     => fetch(`/api/incident/${id}/approve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ proposal_id: pid }) }).then(J),
  deny:        (id, pid, why)=> fetch(`/api/incident/${id}/deny`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ proposal_id: pid, reason: why || '' }) }).then(J),
  decoyProbe:  (value)       => fetch('/api/decoy/probe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: value ?? 1.75 }) }).then(J),
}

export function openStream(onEvent) {
  const es = new EventSource('/api/stream')
  es.onmessage = (m) => {
    try { onEvent(JSON.parse(m.data)) } catch { /* ignore malformed frame */ }
  }
  return es
}

export const fmt = (v, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toFixed(d)
