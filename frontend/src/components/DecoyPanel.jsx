import { useState } from 'react'
import { api, fmt } from '../lib/api.js'

/**
 * Deception panel. Kept visually separate from operational values so a decoy
 * reading can never be mistaken for plant state.
 */
export default function DecoyPanel({ decoy, gateway }) {
  const [busy, setBusy] = useState(false)
  const [last, setLast] = useState(null)
  const active = !!decoy?.active
  const interactions = decoy?.interactions || []

  const probe = async () => {
    setBusy(true)
    try { setLast(await api.decoyProbe(1.75)) } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <h3>Containment &amp; decoy
        <span className="right">
          <span className={`pill ${active ? 'decoy' : ''}`}>
            <span className="dot" />{active ? 'DECOY ACTIVE' : 'decoy inactive'}
          </span>
        </span>
      </h3>

      <div className="kv"><span className="k">Contained clients</span>
        <span className="v">{(gateway?.contained_clients || []).join(', ') || '—'}</span></div>
      <div className="kv"><span className="k">Write authorisation</span>
        <span className="v">{(gateway?.write_acl || []).join(', ') || 'none'}</span></div>
      <div className="kv"><span className="k">Decoy writes received</span>
        <span className="v">{decoy?.writes_received ?? 0}</span></div>

      <div className="divider" />
      <div className={`banner ${active ? 'ok' : 'warn'}`} style={{ marginBottom: 10 }}>
        {active
          ? <>This client <b>is contained</b>. The write should reach only the decoy
              and leave operational state untouched — that is the isolation proof.</>
          : <>This client is <b>not contained</b>, so the write <b>will reach the
              plant</b> and disturb it. That is the honest &ldquo;before&rdquo; half of
              the demo — press it after the agent contains the client to show the
              difference.</>}
      </div>
      <div className="row">
        <button className={active ? 'primary' : ''} onClick={probe} disabled={busy}>
          {busy ? 'sending…' : 'Send test-client write'}
        </button>
        <span className="muted">
          {active ? 'expected: routed to decoy' : 'expected: reaches operational state'}
        </span>
      </div>

      {last && (
        <div className={`banner ${last.operational_state_changed ? 'bad' : 'ok'}`}
             style={{ marginTop: 10 }}>
          <b>Route: {last.route}</b> — operational forwarded value{' '}
          {last.operational_state_changed
            ? <>CHANGED — the forwarded offset moved
                {' '}{fmt(last.operational_offset_before)} → {fmt(last.operational_offset_after)} bar.
                This client is not contained.</>
            : <>unchanged — the forwarded offset is still
                {' '}{fmt(last.operational_offset_after)} bar. The write reached
                only the decoy, which now reads {fmt(last.decoy?.pt101_forwarded_bar)} bar.</>}
        </div>
      )}

      {interactions.length > 0 && (
        <>
          <div className="divider" />
          <div className="muted" style={{ marginBottom: 6 }}>Decoy interaction log (isolated state)</div>
          <div className="scroll" style={{ maxHeight: 130 }}>
            {interactions.slice().reverse().map((i, n) => (
              <div className="kv" key={n}>
                <span className="k mono">{i.client_id} → {i.field}</span>
                <span className="v">{fmt(i.attempted_value)} <span className="muted">(decoy only)</span></span>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="muted" style={{ marginTop: 10 }}>
        Decoy responses are never used as operational feedback. The demo assumes the
        simulator, reference path, gateway and trusted log are uncompromised.
      </div>
    </div>
  )
}
