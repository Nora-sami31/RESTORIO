/**
 * Equipment map: physical water path (solid) versus data/control links
 * (dashed). Shows which reading feeds the controller, which path is contained,
 * and where the isolated decoy sits.
 */
export default function TopologyMap({ sample, gateway, onSelect, selected }) {
  const s = sample || {}
  const fwdAltered = s.pt101_local_bar != null && s.pt101_forwarded_bar != null
    && Math.abs(s.pt101_forwarded_bar - s.pt101_local_bar) > 0.2
  const onTrusted = s.feedback_source === 'PT-102'
  const contained = (gateway?.contained_clients || []).length > 0
  // An altered feed the controller no longer reads is bypassed, not failing.
  const bypassed = fwdAltered && onTrusted
  const decoy = !!s.decoy_active

  const N = ({ x, y, w = 82, h = 34, id, label, sub, tone }) => (
    <g onClick={() => onSelect?.(id)} style={{ cursor: 'pointer' }}>
      <rect x={x} y={y} width={w} height={h} rx="6"
            fill={selected === id ? '#152c33' : '#172230'}
            stroke={selected === id ? '#35c2d6' : (tone || '#22303f')} strokeWidth="1.4" />
      <text x={x + w / 2} y={y + 15} textAnchor="middle" fill="#e6edf3"
            fontSize="10.5" fontFamily="monospace" fontWeight="600">{label}</text>
      <text x={x + w / 2} y={y + 27} textAnchor="middle" fill="#8da2b5" fontSize="9">{sub}</text>
    </g>
  )

  return (
    <div className="panel">
      <h3>Equipment map
        <span className="right muted">solid: water path · dashed: measurement / control</span>
      </h3>
      <svg viewBox="0 0 560 230" style={{ width: '100%', height: 215 }}>
        {/* water path */}
        <path d="M66,52 H150" stroke="#5c7489" strokeWidth="5" fill="none" />
        <path d="M232,52 H470" stroke={ (s.pt102_bar ?? 0) > 6 ? '#c06a4a' : '#5c7489'}
              strokeWidth="5" fill="none" />
        {/* measurement / control links */}
        <path d="M300,70 V112 H196 V130"
              stroke={bypassed ? '#b07cf5' : (fwdAltered ? '#ef5a5a' : '#d3a23a')}
              strokeWidth="1.6" fill="none" strokeDasharray="5 4" />
        <path d="M196,166 H118 V130" stroke={onTrusted ? '#5d7286' : '#8da2b5'}
              strokeWidth="1.6" fill="none" strokeDasharray="5 4" />
        <path d="M420,70 V190 H118 V164" stroke={onTrusted ? '#2ecc87' : '#3f9fd0'}
              strokeWidth={onTrusted ? 2.4 : 1.4} fill="none" strokeDasharray="5 4" />
        {decoy && <path d="M278,147 H360" stroke="#e0683c" strokeWidth="1.8"
                        fill="none" strokeDasharray="4 3" />}

        {N({ x: 14, y: 35, w: 52, h: 34, id: 'TK-101', label: 'TK-101', sub: `${Math.round(s.tank_level_pct ?? 0)}%` })}
        {N({ x: 150, y: 35, id: 'P-101', label: 'P-101', sub: `${Math.round(s.pump_output_pct ?? 0)}%`,
             tone: (s.pump_output_pct ?? 0) > 88 ? '#f0b429' : null })}
        {N({ x: 262, y: 35, w: 76, id: 'PT-101', label: 'PT-101',
             sub: bypassed ? 'primary · bypassed' : 'primary',
             tone: bypassed ? '#b07cf5' : (fwdAltered ? '#ef5a5a' : null) })}
        {N({ x: 382, y: 35, w: 76, id: 'PT-102', label: 'PT-102', sub: 'reference',
             tone: s.pt102_bar == null ? '#ef5a5a' : (s.pt102_quality !== 'good' ? '#f0b429' : null) })}
        {N({ x: 155, y: 130, w: 82, id: 'telemetry-gateway', label: 'Gateway',
             sub: contained ? 'containing'
                : (bypassed ? 'altered · off control'
                : (fwdAltered ? 'value altered' : 'passthrough')),
             tone: contained || bypassed ? '#b07cf5'
                 : (fwdAltered ? '#ef5a5a' : null) })}
        {N({ x: 77, y: 130, w: 82, id: 'PLC-101', label: 'PLC-101',
             sub: s.controller_mode === 'safe_state' ? 'safe state' : `fb ${s.feedback_source || '—'}`,
             tone: s.controller_mode === 'safe_state' ? '#f0b429' : (onTrusted ? '#2ecc87' : null) })}
        {N({ x: 360, y: 130, w: 86, id: 'decoy-endpoint', label: 'Decoy',
             sub: decoy ? 'ACTIVE · isolated' : 'inactive',
             tone: decoy ? '#e0683c' : null })}

        <text x="300" y="100" textAnchor="middle"
              fill={bypassed ? '#b07cf5' : (fwdAltered ? '#ef5a5a' : '#8da2b5')}
              fontSize="9" fontFamily="monospace">
          {bypassed ? 'forwarded ≠ local · not in control'
            : (fwdAltered ? 'forwarded ≠ local' : 'forwarded telemetry')}
        </text>
        <text x="468" y="186" textAnchor="end" fill={onTrusted ? '#2ecc87' : '#5d7286'}
              fontSize="9" fontFamily="monospace">
          {onTrusted ? 'CONTROL ON TRUSTED PATH' : 'independent reference'}
        </text>
      </svg>
    </div>
  )
}
