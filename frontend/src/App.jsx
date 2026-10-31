import { useCallback, useEffect, useRef, useState } from 'react'
import Splitter from './components/Splitter.jsx'
import Observability from './components/Observability.jsx'
import IncidentHistory from './components/IncidentHistory.jsx'
import { api, fmt, openStream } from './lib/api.js'
import Station3D from './components/Station3D.jsx'
import TopologyMap from './components/TopologyMap.jsx'
import Trends from './components/Trends.jsx'
import AgentPanel from './components/AgentPanel.jsx'
import IncidentInspector, { ApprovalBanner } from './components/IncidentInspector.jsx'
import DecoyPanel from './components/DecoyPanel.jsx'

const ASSETS = [
  ['TK-101', 'Source tank'], ['P-101', 'Transfer pump'],
  ['PT-101', 'Primary transmitter'], ['PT-102', 'Reference transmitter'],
  ['PLC-101', 'Station controller'], ['telemetry-gateway', 'Telemetry gateway'],
  ['decoy-endpoint', 'Decoy endpoint'],
]

export default function App() {
  const [sample, setSample] = useState(null)
  const [samples, setSamples] = useState([])
  const [markers, setMarkers] = useState([])
  const [events, setEvents] = useState([])
  const [state, setState] = useState(null)
  const [incident, setIncident] = useState(null)
  const [scenarios, setScenarios] = useState([])
  const [truth, setTruth] = useState(null)
  const [truthSeries, setTruthSeries] = useState([])
  const [showTruth, setShowTruth] = useState(false)
  const [selected, setSelected] = useState(null)
  const [assetDetail, setAssetDetail] = useState(null)
  const [gateway, setGateway] = useState(null)
  const [decoy, setDecoy] = useState(null)
  const [provider, setProvider] = useState(null)
  const [connected, setConnected] = useState(false)
  const incidentId = useRef(null)

  // Vertical weights for the centre column, as fractions that sum to 1.
  const DEFAULT_SPLIT = { scene: 0.56, map: 0.26, trends: 0.18 }
  const MIN = { scene: 0.14, map: 0.10, trends: 0.10 }
  const [split, setSplit] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('restore.split') || 'null')
      if (saved && typeof saved.scene === 'number') return saved
    } catch { /* storage may be unavailable */ }
    return DEFAULT_SPLIT
  })
  const [maximized, setMaximized] = useState(false)
  // Collapsing the inspector gives the station view the full width. The
  // choice is per-viewer convenience, so it lives in browser storage and
  // the app renders correctly when that is unavailable.
  const [inspectorOpen, setInspectorOpen] = useState(() => {
    try { return localStorage.getItem('restorio.inspector') !== 'closed' }
    catch { return true }
  })
  useEffect(() => {
    try { localStorage.setItem('restorio.inspector', inspectorOpen ? 'open' : 'closed') }
    catch { /* private window, blocked storage — the layout still works */ }
  }, [inspectorOpen])
  const centerRef = useRef(null)

  useEffect(() => {
    try { localStorage.setItem('restore.split', JSON.stringify(split)) } catch { /* ignore */ }
  }, [split])

  // Move weight between two adjacent panes, never below their minimums.
  const resize = useCallback((a, b) => (delta) => {
    setSplit((prev) => {
      const next = { ...prev }
      const give = Math.min(Math.max(delta, -(prev[a] - MIN[a])), prev[b] - MIN[b])
      next[a] = prev[a] + give
      next[b] = prev[b] - give
      return next
    })
  }, [])

  const resetSplit = useCallback(() => setSplit(DEFAULT_SPLIT), [])

  useEffect(() => {
    if (!maximized) return
    const esc = (e) => e.key === 'Escape' && setMaximized(false)
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [maximized])

  // ---- bootstrap ---------------------------------------------------------
  useEffect(() => {
    api.scenarios().then((d) => setScenarios(d.scenarios)).catch(() => {})
    api.provider().then(setProvider).catch(() => {})
    api.history().then((d) => { setSamples(d.samples || []); setMarkers(d.markers || []) }).catch(() => {})
    refreshSide()
  }, [])

  const refreshSide = useCallback(() => {
    api.state().then(setState).catch(() => {})
    api.gateway().then(setGateway).catch(() => {})
    api.decoy().then(setDecoy).catch(() => {})
  }, [])

  // ---- live event stream --------------------------------------------------
  useEffect(() => {
    const es = openStream((evt) => {
      setConnected(true)
      if (evt.kind === 'tick') {
        setSample(evt.payload)
        setSamples((prev) => [...prev.slice(-700), evt.payload])
        return
      }
      if (evt.kind === 'snapshot') { setState(evt.payload); return }
      setEvents((prev) => [...prev.slice(-400), evt])

      if (evt.kind === 'marker') setMarkers((p) => [...p, evt.payload])
      if (evt.kind === 'incident_detected') incidentId.current = evt.payload.incident_id
      if (['incident_state', 'incident_timeline', 'incident_closed', 'approval_requested',
           'approval_granted', 'execution', 'verification', 'test_report',
           'agent_finished'].includes(evt.kind)) {
        const id = evt.payload?.incident_id || incidentId.current
        if (id && !viewingHistoricRef.current) {
          api.incident(id).then(setIncident).catch(() => {})
        }
      }
      if (evt.kind === 'incident_closed') refreshIncidents()
      if (['execution', 'decoy_probe', 'incident_closed', 'scenario_loaded',
           'autonomy_changed', 'detection_rearmed', 'detection_held',
           'monitoring_result'].includes(evt.kind)) refreshSide()
      if (evt.kind === 'scenario_loaded') {
        setEvents([evt]); setIncident(null); setMarkers([]); setSamples([])
        setTruthSeries([]); incidentId.current = null; setViewingHistoric(null)
        refreshIncidents()
      }
    })
    es.onerror = () => setConnected(false)
    return () => es.close()
  }, [refreshSide])

  // ---- audience-only ground truth -----------------------------------------
  useEffect(() => {
    if (!showTruth) return
    let alive = true
    const poll = async () => {
      try {
        const t = await api.truth()
        if (!alive) return
        setTruth(t)
        setTruthSeries((p) => [...p.slice(-700), t.true_pressure_bar])
      } catch { /* ignore */ }
    }
    poll()
    const h = setInterval(poll, 400)
    return () => { alive = false; clearInterval(h) }
  }, [showTruth])

  // The asset inspector carries LIVE readings -- PT-102's value, quality and
  // age, the gateway's forwarding state -- not just static metadata. Fetching
  // it only when the configuration revision changes left it frozen on
  // whatever the station was doing at that moment, so after an incident
  // settled it kept showing the reading from when the fault was detected.
  useEffect(() => {
    if (!selected) { setAssetDetail(null); return }
    let alive = true
    const pull = () => api.asset(selected)
      .then((d) => { if (alive) setAssetDetail(d) })
      .catch(() => { if (alive) setAssetDetail(null) })
    pull()
    const h = setInterval(pull, 1500)
    return () => { alive = false; clearInterval(h) }
  }, [selected])

  const [generating, setGenerating] = useState(false)
  const [historyRows, setHistoryRows] = useState([])
  const [viewingHistoric, setViewingHistoric] = useState(null)
  // The event-stream callback is built once, so reading the state variable
  // inside it would capture its FIRST value and never see a change. The ref
  // is read at call time, which is what the guard actually needs.
  const viewingHistoricRef = useRef(null)
  useEffect(() => { viewingHistoricRef.current = viewingHistoric }, [viewingHistoric])

  // Reload the open incident AND the audit trail. Without this a refresh
  // during an approval pause leaves the operator with no approval controls
  // until some unrelated event happens to arrive.
  const refreshIncidents = useCallback(async () => {
    try {
      const d = await api.incidents()
      setHistoryRows(d.history || [])
      api.forecast().then(setForecast).catch(() => {})
      if (d.live?.length) {
        const open = d.live[d.live.length - 1]
        incidentId.current = open.incident_id
        if (!viewingHistoricRef.current) setIncident(open)
      }
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { refreshIncidents() }, [refreshIncidents])

  // The forecast statistics are free to read, so they load with the trail.
  // Producing the agent's forecast costs a provider call and happens only
  // when the operator asks for it.
  const [forecast, setForecast] = useState(null)
  const loadForecast = useCallback(async () => {
    try { setForecast(await api.forecast()) } catch { /* advisory only */ }
  }, [])
  const runForecast = useCallback(async () => {
    try { setForecast(await api.forecast()) } catch { /* ignore */ }
    try {
      await api.runForecast()
      setForecast(await api.forecast())
    } catch { /* leave the statistics showing */ }
  }, [])
  useEffect(() => { loadForecast() }, [loadForecast])

  const openHistoric = useCallback(async (id) => {
    try {
      const rec = await api.incident(id)
      setViewingHistoric(id)
      setIncident(rec)
    } catch { /* ignore */ }
  }, [])

  const backToLive = useCallback(async () => {
    setViewingHistoric(null)
    if (incidentId.current) {
      try { setIncident(await api.incident(incidentId.current)) } catch { /* ignore */ }
    } else setIncident(null)
  }, [])

  const generateIncident = async () => {
    setGenerating(true)
    setEvents([]); setIncident(null); setMarkers([]); setSamples([]); setTruthSeries([])
    incidentId.current = null
    try { await api.generate({ recoverable: true }) } finally { setGenerating(false) }
    refreshSide()
  }

  const loadScenario = async (k) => {
    setEvents([]); setIncident(null); setMarkers([]); setSamples([]); setTruthSeries([])
    incidentId.current = null
    await api.loadScenario(k)
    refreshSide()
  }

  const s = sample || {}
  const ref = s.pt102_bar, fwd = s.pt101_forwarded_bar
  const local = s.pt101_local_bar

  // Compare the measurement the controller is ACTUALLY using against an
  // independent one. Once a feed has been quarantined and control has moved
  // to the trusted path, that feed is not required to agree -- which is what
  // verification already says. Keeping it in the headline made a recovered
  // station read as broken.
  const onTrusted = s.feedback_source === 'PT-102'
  const quarantined = onTrusted && local != null && fwd != null
                      && Math.abs(fwd - local) > 0.2
  const controlling = onTrusted ? ref : fwd
  const crossCheck = onTrusted ? local : ref
  const gap = controlling != null && crossCheck != null
    ? Math.abs(controlling - crossCheck) : null
  const tone = gap == null ? '' : (gap > 0.45 ? 'bad' : (gap > 0.2 ? 'warn' : 'ok'))
  const stale = s.pt102_quality && s.pt102_quality !== 'good'

  if (maximized) {
    return (
      <div className="maxwrap">
        <div className="maxbar">
          <div className="brand">
          <img src="/logo.png" alt="" width="22" height="22" />
          <span className="wordmark">RE<span>STORIO</span></span>
        </div>
          <span className={`pill ${connected ? 'live' : 'bad'}`}>
            <span className="dot blink" />station view
          </span>
          <div className="stat"><span className="k">forwarded</span>
            <span className="v">{fmt(fwd)} bar</span></div>
          <div className="stat"><span className="k">reference</span>
            <span className="v" style={{ color: ref == null ? '#ef5a5a' : undefined }}>
              {ref == null ? 'no signal' : `${fmt(ref)} bar`}</span></div>
          <div className="stat"><span className="k">disagreement</span>
            <span className="v" style={{ color: tone === 'bad' ? '#ef5a5a' : (tone === 'warn' ? '#f0b429' : '#2ecc87') }}>
              {gap == null ? '—' : `${fmt(gap)} bar`}</span></div>
          <div className="stat"><span className="k">pump</span>
            <span className="v">{fmt(s.pump_output_pct, 0)} %</span></div>
          {s.controller_mode === 'safe_state' &&
            <span className="pill warn"><span className="dot" />SAFE STATE</span>}
          {s.decoy_active && <span className="pill decoy"><span className="dot" />decoy active</span>}
          <div className="spacer" />
          {showTruth && truth && (
            <span className="pill warn">
              <span className="dot" />true pressure {fmt(truth.true_pressure_bar)} bar
            </span>
          )}
          <button className={showTruth ? 'primary' : 'ghost'} onClick={() => setShowTruth((v) => !v)}>
            {showTruth ? 'hide' : 'show'} ground truth
          </button>
        </div>
        <Station3D sample={sample} truth={truth} showTruth={showTruth}
                   gateway={gateway} onSelect={setSelected} selected={selected}
                   maximized onToggleMaximize={() => setMaximized(false)} />
        {!viewingHistoric && (
          <ApprovalBanner incident={incident} pinned
                          onRefresh={() => incidentId.current &&
                            api.incident(incidentId.current).then(setIncident).catch(() => {})} />
        )}
      </div>
    )
  }

  return (
    <div className="app">
      {/* ---------------- top strip ---------------- */}
      <div className="topbar">
        <div className="brand">
          <img src="/logo.png" alt="" width="22" height="22" />
          <span className="wordmark">RE<span>STORIO</span></span>
        </div>
        <div className="sep" />
        <span className={`pill ${connected ? 'live' : 'bad'}`}>
          <span className="dot blink" />{connected ? 'LIVE SIMULATION' : 'disconnected'}
        </span>
        <div className="stat">
          <span className="k">{onTrusted ? 'controlling (PT-102)' : 'forwarded'}</span>
          <span className="v">{fmt(controlling)} bar</span></div>
        <div className="stat">
          <span className="k">{onTrusted ? 'local at PT-101' : 'reference'}</span>
          <span className="v" style={{ color: crossCheck == null ? '#ef5a5a' : undefined }}>
            {crossCheck == null ? 'no signal' : `${fmt(crossCheck)} bar`}</span></div>
        <div className="stat"><span className="k">disagreement</span>
          <span className="v" style={{ color: tone === 'bad' ? '#ef5a5a' : (tone === 'warn' ? '#f0b429' : '#2ecc87') }}>
            {gap == null ? '—' : `${fmt(gap)} bar`}</span></div>
        <div className="stat"><span className="k">pump</span>
          <span className="v">{fmt(s.pump_output_pct, 0)} %</span></div>
        <div className="stat"><span className="k">rev</span>
          <span className="v">{s.config_revision ?? '—'}</span></div>
        {stale && <span className="pill warn"><span className="dot" />reference {s.pt102_quality}</span>}
        {s.controller_mode === 'safe_state' && <span className="pill warn"><span className="dot" />SAFE STATE</span>}
        {state?.detection_armed === false && (
          <span className="pill warn" title={state?.detection_held_reason || ''}>
            <span className="dot" />detection held
          </span>
        )}
        {quarantined && (
          <span className="pill contain" title={`The forwarded feed still reads ${fmt(fwd)} bar and is no longer used for control.`}>
            <span className="dot" />PT-101 feed quarantined
          </span>
        )}
        {s.decoy_active && <span className="pill decoy"><span className="dot" />decoy active</span>}

        <div className="spacer" />
        <select value={scenarios.some((sc) => sc.key === state?.scenario_running)
                        ? state.scenario_running : ''}
                onChange={(e) => e.target.value && loadScenario(e.target.value)}>
          {state?.scenario_running === 'generated' &&
            <option value="">— generated incident —</option>}
          {scenarios.map((sc) => <option key={sc.key} value={sc.key}>{sc.title}</option>)}
        </select>
        <button onClick={generateIncident} disabled={generating}
                title="Compose a random, well-posed incident. Nobody authored it — not us, not the rules.">
          {generating ? 'drawing…' : '⚄ generate'}
        </button>
        <select
          title="How much the agent may do without a human. Investigation, diagnosis, testing and verification are always autonomous; this governs state-changing writes only."
          value={state?.settings?.autonomy || 'guarded'}
          onChange={async (e) => { await api.autonomy(e.target.value); refreshSide() }}>
          <option value="full">autonomy: full</option>
          <option value="guarded">autonomy: guarded</option>
          <option value="supervised">autonomy: supervised</option>
        </select>
        <span className={`pill ${provider?.ok ? 'ok' : 'warn'}`}>
          <span className="dot" />{provider?.provider || '…'}
        </span>
        <button className={showTruth ? 'primary' : 'ghost'} onClick={() => setShowTruth((v) => !v)}>
          {showTruth ? 'hide' : 'show'} ground truth
        </button>
      </div>

      {showTruth && truth && (
        <div className="banner warn" style={{ margin: 0, borderRadius: 0, borderLeft: 0, borderRight: 0 }}>
          <b>AUDIENCE OVERLAY — hidden from the agent.</b>{' '}
          True line pressure <b>{fmt(truth.true_pressure_bar)} bar</b> ·
          scenario <code>{truth.scenario}</code> ·
          injected faults: <code>{truth.active_faults.join(', ') || 'none'}</code>
          {truth.generated && <>
            {' '}· <b>generated #{truth.generated.seed}</b>, expected{' '}
            <code>{truth.generated.expected_states.join(' / ')}</code>
          </>}
          {truth.verdict && (
            <div style={{ marginTop: 6, fontWeight: 700,
                          color: truth.verdict.correct ? '#2ecc87' : '#ef5a5a' }}>
              {truth.verdict.correct
                ? (truth.verdict.escalation_was_correct
                    ? `✓ CORRECT — escalating was the right answer here (${truth.verdict.rationale})`
                    : '✓ CORRECT — the agent reached the expected outcome')
                : `✗ MISMATCH — expected ${truth.verdict.expected.join(' / ')}, got ${truth.verdict.actual}`}
              {truth.verdict.unsupported_actions?.length > 0 &&
                ` · took unsupported: ${truth.verdict.unsupported_actions.join(', ')}`}
            </div>
          )}
          {truth.tripped && <> · <b style={{ color: '#ef5a5a' }}>PROTECTIVE TRIP</b></>}
        </div>
      )}

      {/* ---------------- body ---------------- */}
      <div className={`main${inspectorOpen ? '' : ' inspector-hidden'}`}>
        {/* left: assets */}
        <div className="col">
          <div className="panel">
            <h3>Assets</h3>
            {ASSETS.map(([id, name]) => {
              const v = {
                'TK-101': `${fmt(s.tank_level_pct, 0)}%`,
                'P-101': `${fmt(s.pump_output_pct, 0)}%`,
                'PT-101': `${fmt(fwd)}`,
                'PT-102': ref == null ? '—' : `${fmt(ref)}`,
                'PLC-101': `r${s.config_revision ?? '—'}`,
                'telemetry-gateway': (gateway?.contained_clients || []).length ? 'contained' : 'open',
                'decoy-endpoint': s.decoy_active ? 'active' : 'off',
              }[id]
              return (
                <div key={id} className={`asset ${selected === id ? 'sel' : ''}`}
                     onClick={() => setSelected(id === selected ? null : id)}>
                  <div>
                    <div className="nm">{id}</div>
                    <div className="ro">{name}</div>
                  </div>
                  <div className="val">{v}</div>
                </div>
              )
            })}
          </div>

          <Observability />

          {assetDetail && (
            <div className="panel">
              <h3>{assetDetail.asset_id}</h3>
              <div className="muted" style={{ marginBottom: 8 }}>{assetDetail.role}</div>
              {assetDetail.installed_device && (
                <>
                  <div className="muted">Installed device</div>
                  <div className="kv"><span className="k">model</span>
                    <span className="v">{assetDetail.installed_device.model}</span></div>
                  <div className="kv"><span className="k">range</span>
                    <span className="v">{assetDetail.installed_device.range_min_bar}–
                      {assetDetail.installed_device.range_max_bar} bar</span></div>
                  <div className="muted" style={{ marginTop: 8 }}>Configured scaling</div>
                  <div className="kv"><span className="k">range</span>
                    <span className="v" style={{
                      color: assetDetail.configured_scaling.range_max_bar !==
                             assetDetail.installed_device.range_max_bar ? '#ef5a5a' : '#2ecc87' }}>
                      {assetDetail.configured_scaling.range_min_bar}–
                      {assetDetail.configured_scaling.range_max_bar} bar</span></div>
                </>
              )}
              {assetDetail.config && Object.entries(assetDetail.config).map(([k, v]) => (
                <div className="kv" key={k}><span className="k">{k}</span>
                  <span className="v">{String(v)}</span></div>
              ))}
              {assetDetail.latest && (
                <>
                  <div className="kv"><span className="k">value</span>
                    <span className="v">{fmt(assetDetail.latest.value)} bar</span></div>
                  <div className="kv"><span className="k">quality</span>
                    <span className="v">{assetDetail.latest.quality}</span></div>
                  <div className="kv"><span className="k">age</span>
                    <span className="v">{fmt(assetDetail.latest.age_s, 1)} s</span></div>
                </>
              )}
              {assetDetail.write_acl && (
                <>
                  <div className="kv"><span className="k">write ACL</span>
                    <span className="v">{assetDetail.write_acl.join(', ') || 'none'}</span></div>
                  <div className="kv"><span className="k">forwarded altered</span>
                    <span className="v">{String(assetDetail.forwarded_field_modified)}</span></div>
                </>
              )}
            </div>
          )}
        </div>

        {/* centre: 3D + map + trends, each resizable */}
        <div className="col center" ref={centerRef}>
          <div className="pane" style={{ flex: `${split.scene} 1 0`, minHeight: 140 }}>
            <Station3D sample={sample} truth={truth} showTruth={showTruth}
                       gateway={gateway} onSelect={setSelected} selected={selected}
                       maximized={false} onToggleMaximize={() => setMaximized(true)} />
          </div>
          <Splitter containerRef={centerRef} onReset={resetSplit}
                    label="Resize the station view and the equipment map"
                    onResize={resize('scene', 'map')} />
          <div className="pane" style={{ flex: `${split.map} 1 0`, minHeight: 110 }}>
            <TopologyMap sample={sample} gateway={gateway}
                         onSelect={setSelected} selected={selected} />
          </div>
          <Splitter containerRef={centerRef} onReset={resetSplit}
                    label="Resize the equipment map and the trends"
                    onResize={resize('map', 'trends')} />
          <div className="pane" style={{ flex: `${split.trends} 1 0`, minHeight: 110 }}>
            <Trends samples={samples} markers={markers} settings={state?.settings}
                    showTruth={showTruth} truthSeries={truthSeries} />
          </div>
        </div>

        {/* right: incident + agent + decoy */}
        {inspectorOpen && (
        <div className="col" style={{ display: 'flex', flexDirection: 'column' }}>
          <IncidentInspector incident={incident} historic={!!viewingHistoric}
                             onRefresh={() => incidentId.current &&
                               api.incident(incidentId.current).then(setIncident).catch(() => {})} />
          <AgentPanel events={events} incident={incident} />
          <DecoyPanel decoy={decoy} gateway={gateway} />
          <IncidentHistory rows={historyRows} viewing={viewingHistoric}
                           onOpen={openHistoric} onBackToLive={backToLive}
                           forecast={forecast} onRunForecast={runForecast} />
        </div>
        )}
      </div>

      <button className={`panel-toggle${inspectorOpen ? '' : ' closed'}`}
              onClick={() => setInspectorOpen((v) => !v)}
              aria-label={inspectorOpen ? 'Hide the incident panel' : 'Show the incident panel'}
              title={inspectorOpen
                ? 'Hide the incident panel and widen the station view'
                : 'Show the incident panel'}>
        {inspectorOpen ? '›' : '‹'}
      </button>

      {/* An approval pause stops the agent, so the control follows the
          operator out of the panel rather than hiding with it. */}
      {!inspectorOpen && !viewingHistoric && (
        <ApprovalBanner incident={incident} pinned
                        onRefresh={() => incidentId.current &&
                          api.incident(incidentId.current).then(setIncident).catch(() => {})} />
      )}
    </div>
  )
}
