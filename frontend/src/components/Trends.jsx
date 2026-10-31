import { fmt } from '../lib/api.js'

/**
 * Primary vs reference pressure, pump output, the target band, and markers for
 * detection, application and verified recovery.
 */
export default function Trends({ samples, markers, settings, showTruth, truthSeries }) {
  const W = 1000, H = 170, PAD_L = 38, PAD_R = 10, PAD_T = 12, PAD_B = 20
  const data = samples.slice(-420)
  if (data.length < 2) {
    return <div className="panel trends"><h3>Trends</h3>
      <div className="muted">Waiting for samples…</div></div>
  }

  const t0 = data[0].t, t1 = data[data.length - 1].t || t0 + 1
  const span = Math.max(t1 - t0, 1)
  const vals = []
  data.forEach((s) => {
    ;[s.pt101_forwarded_bar, s.pt101_local_bar, s.pt102_bar].forEach((v) => {
      if (v != null) vals.push(v)
    })
  })
  if (showTruth) truthSeries.forEach((v) => v != null && vals.push(v))
  const setpoint = settings?.setpoint_bar ?? 4
  vals.push(setpoint + 1, 0)
  const vmax = Math.max(...vals) * 1.08, vmin = 0

  const X = (t) => PAD_L + ((t - t0) / span) * (W - PAD_L - PAD_R)
  const Y = (v) => H - PAD_B - ((v - vmin) / (vmax - vmin || 1)) * (H - PAD_T - PAD_B)

  const path = (key, fn) => {
    let d = '', pen = false
    data.forEach((s) => {
      const v = fn ? fn(s) : s[key]
      if (v == null) { pen = false; return }
      d += `${pen ? 'L' : 'M'}${X(s.t).toFixed(1)},${Y(v).toFixed(1)}`
      pen = true
    })
    return d
  }

  const band = settings?.target_band_bar ?? 0.3
  const MARK = { detected: '#ef5a5a', applied: '#b07cf5', verified: '#2ecc87',
                 verify_failed: '#f0b429', scenario_loaded: '#5d7286' }

  return (
    <div className="panel trends">
      <h3>Trends
        <span className="right muted">
          <b style={{ color: '#d3a23a' }}>━</b> forwarded to PLC &nbsp;
          <b style={{ color: '#9fb8cc' }}>━</b> local at PT-101 &nbsp;
          <b style={{ color: '#3f9fd0' }}>━</b> PT-102 reference &nbsp;
          {showTruth && <><b style={{ color: '#ef5a5a' }}>┅</b> true pressure (audience)</>}
        </span>
      </h3>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <rect x={PAD_L} y={Y(setpoint + band)} width={W - PAD_L - PAD_R}
              height={Math.max(1, Y(setpoint - band) - Y(setpoint + band))}
              fill="#2ecc87" opacity="0.1" />
        <line x1={PAD_L} x2={W - PAD_R} y1={Y(setpoint)} y2={Y(setpoint)}
              stroke="#2ecc87" strokeWidth="1" strokeDasharray="5 4" opacity="0.6" />
        {[0, 2, 4, 6, 8].filter((v) => v <= vmax).map((v) => (
          <g key={v}>
            <line x1={PAD_L} x2={W - PAD_R} y1={Y(v)} y2={Y(v)} stroke="#22303f" strokeWidth="0.7" />
            <text x={4} y={Y(v) + 3.5} fill="#5d7286" fontSize="10" fontFamily="monospace">{v}</text>
          </g>
        ))}
        {markers.filter((m) => MARK[m.kind] && m.t >= t0 && m.t <= t1).map((m, i) => (
          <g key={i}>
            <line x1={X(m.t)} x2={X(m.t)} y1={PAD_T} y2={H - PAD_B}
                  stroke={MARK[m.kind]} strokeWidth="1.2" opacity="0.75" />
            <circle cx={X(m.t)} cy={PAD_T} r="3" fill={MARK[m.kind]} />
          </g>
        ))}
        {showTruth && (
          <path d={(() => {
            let d = '', pen = false
            data.forEach((s, i) => {
              const v = truthSeries[truthSeries.length - data.length + i]
              if (v == null) { pen = false; return }
              d += `${pen ? 'L' : 'M'}${X(s.t).toFixed(1)},${Y(v).toFixed(1)}`; pen = true
            })
            return d
          })()} fill="none" stroke="#ef5a5a" strokeWidth="1.6" strokeDasharray="4 3" opacity="0.85" />
        )}
        <path d={path('pt102_bar')} fill="none" stroke="#3f9fd0" strokeWidth="1.7" />
        <path d={path('pt101_local_bar')} fill="none" stroke="#9fb8cc" strokeWidth="1.2" opacity="0.75" />
        <path d={path('pt101_forwarded_bar')} fill="none" stroke="#d3a23a" strokeWidth="2" />
        <text x={W - PAD_R} y={H - 5} textAnchor="end" fill="#5d7286" fontSize="9"
              fontFamily="monospace">{fmt(span, 0)} s window · simulated process</text>
      </svg>
    </div>
  )
}
