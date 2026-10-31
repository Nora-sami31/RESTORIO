import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { fmt } from '../lib/api.js'

/**
 * Live 3D view of the pumping station.
 *
 * Labels display real simulator readings; the pump animates from backend
 * state; tank level follows the model. Clicking an object selects that asset,
 * using the same asset IDs as the map, telemetry and incident record.
 */
export default function Station3D({ sample, truth, showTruth, gateway, onSelect,
                                   selected, maximized, onToggleMaximize }) {
  const mount = useRef(null)
  const live = useRef({ sample, truth, showTruth, gateway, selected })
  const [tags, setTags] = useState([])
  live.current = { sample, truth, showTruth, gateway, selected }

  useEffect(() => {
    const el = mount.current
    if (!el) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x080d12)
    scene.fog = new THREE.Fog(0x080d12, 22, 52)

    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 200)
    camera.position.set(9.5, 6.2, 11)
    camera.lookAt(0, 1.3, 0)

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2))
    el.appendChild(renderer.domElement)

    scene.add(new THREE.AmbientLight(0x89a6bd, 0.75))
    const key = new THREE.DirectionalLight(0xffffff, 1.5)
    key.position.set(8, 12, 7)
    scene.add(key)
    const rim = new THREE.DirectionalLight(0x35c2d6, 0.55)
    rim.position.set(-7, 4, -6)
    scene.add(rim)

    // ground
    const grid = new THREE.GridHelper(40, 40, 0x1b2a38, 0x131e29)
    grid.position.y = -0.01
    scene.add(grid)

    const pickable = []
    const mat = (c, rough = 0.55, metal = 0.35) =>
      new THREE.MeshStandardMaterial({ color: c, roughness: rough, metalness: metal })

    // ---- TK-101 tank --------------------------------------------------
    const tankG = new THREE.Group()
    tankG.position.set(-5.4, 0, 0)
    const shell = new THREE.Mesh(new THREE.CylinderGeometry(1.5, 1.5, 3.4, 40, 1, true),
      new THREE.MeshStandardMaterial({ color: 0x9fb8cc, roughness: 0.25, metalness: 0.5,
        transparent: true, opacity: 0.3, side: THREE.DoubleSide }))
    shell.position.y = 1.7
    const water = new THREE.Mesh(new THREE.CylinderGeometry(1.42, 1.42, 1, 36),
      new THREE.MeshStandardMaterial({ color: 0x2f88c4, roughness: 0.2, metalness: 0.1,
        transparent: true, opacity: 0.85 }))
    const base = new THREE.Mesh(new THREE.CylinderGeometry(1.55, 1.55, 0.18, 40), mat(0x4a5f73))
    base.position.y = 0.09
    tankG.add(shell, water, base)
    tankG.userData.assetId = 'TK-101'
    shell.userData.assetId = 'TK-101'
    scene.add(tankG)
    pickable.push(shell, base)

    // ---- pipes ---------------------------------------------------------
    const pipeMat = mat(0x5c7489, 0.4, 0.75)
    const mkPipe = (len, rot, pos) => {
      const p = new THREE.Mesh(new THREE.CylinderGeometry(0.21, 0.21, len, 20), pipeMat)
      p.rotation.set(...rot); p.position.set(...pos)
      scene.add(p); return p
    }
    mkPipe(2.6, [0, 0, Math.PI / 2], [-3.9, 0.95, 0])     // tank -> pump
    const discharge = mkPipe(6.4, [0, 0, Math.PI / 2], [0.9, 0.95, 0])  // pump -> out
    mkPipe(1.5, [0, 0, 0], [4.1, 1.65, 0])

    // ---- P-101 pump ------------------------------------------------------
    const pumpG = new THREE.Group()
    pumpG.position.set(-2.3, 0.95, 0)
    const casing = new THREE.Mesh(new THREE.SphereGeometry(0.72, 28, 20), mat(0x3f7f6a, 0.4, 0.6))
    const motor = new THREE.Mesh(new THREE.CylinderGeometry(0.42, 0.42, 1.25, 26), mat(0x55697d, 0.45, 0.7))
    motor.rotation.z = Math.PI / 2; motor.position.x = 1.0
    const fan = new THREE.Group()
    for (let i = 0; i < 6; i++) {
      const b = new THREE.Mesh(new THREE.BoxGeometry(0.055, 0.52, 0.17), mat(0x8fa6ba, 0.5, 0.4))
      b.position.y = 0.27; b.rotation.x = (i / 6) * Math.PI * 2
      const h = new THREE.Group(); h.add(b); h.rotation.x = (i / 6) * Math.PI * 2
      fan.add(h)
    }
    fan.rotation.z = Math.PI / 2; fan.position.x = 1.72
    const skid = new THREE.Mesh(new THREE.BoxGeometry(2.9, 0.22, 1.5), mat(0x33404e))
    skid.position.set(0.45, -0.84, 0)
    pumpG.add(casing, motor, fan, skid)
    casing.userData.assetId = 'P-101'; motor.userData.assetId = 'P-101'
    scene.add(pumpG); pickable.push(casing, motor)

    // ---- transmitters -----------------------------------------------------
    const mkSensor = (x, z, id, colour) => {
      const g = new THREE.Group()
      g.position.set(x, 0.95, z)
      const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.075, 0.075, 0.95, 14), mat(0x6f8599))
      stem.position.y = 0.48
      const head = new THREE.Mesh(new THREE.CylinderGeometry(0.33, 0.33, 0.42, 22), mat(colour, 0.35, 0.55))
      head.position.y = 1.08
      const cap = new THREE.Mesh(new THREE.SphereGeometry(0.21, 18, 12), mat(0xcfe0ee, 0.2, 0.2))
      cap.position.y = 1.36
      g.add(stem, head, cap)
      head.userData.assetId = id; cap.userData.assetId = id; stem.userData.assetId = id
      scene.add(g); pickable.push(head, cap, stem)
      return { group: g, head }
    }
    const pt101 = mkSensor(1.4, 0, 'PT-101', 0xd3a23a)
    const pt102 = mkSensor(3.6, 0, 'PT-102', 0x3f9fd0)

    // ---- PLC + gateway cabinets --------------------------------------------
    const mkBox = (x, z, w, colour, id) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, 1.5, 0.8), mat(colour, 0.6, 0.3))
      m.position.set(x, 0.75, z); m.userData.assetId = id
      scene.add(m); pickable.push(m); return m
    }
    const plc = mkBox(-3.4, -5.8, 1.7, 0x3d5064, 'PLC-101')
    const gw = mkBox(0.4, -5.8, 1.7, 0x44405f, 'telemetry-gateway')
    const decoyBox = mkBox(4.4, -5.8, 1.5, 0x4a3024, 'decoy-endpoint')

    // signal links (dashed = measurement / control)
    const mkLink = (a, b, colour) => {
      const m = new THREE.LineDashedMaterial({ color: colour, dashSize: 0.17, gapSize: 0.13, linewidth: 1 })
      const g = new THREE.BufferGeometry().setFromPoints([a, b])
      const l = new THREE.Line(g, m); l.computeLineDistances(); scene.add(l); return l
    }
    const linkPT101 = mkLink(new THREE.Vector3(1.4, 2.0, 0), new THREE.Vector3(0.4, 1.5, -5.8), 0xd3a23a)
    const linkPT102 = mkLink(new THREE.Vector3(3.6, 2.0, 0), new THREE.Vector3(-3.4, 1.5, -5.8), 0x3f9fd0)
    const linkGwPlc = mkLink(new THREE.Vector3(0.4, 1.5, -5.8), new THREE.Vector3(-3.4, 1.5, -5.8), 0x6b7f93)
    const linkDecoy = mkLink(new THREE.Vector3(0.4, 1.5, -5.8), new THREE.Vector3(4.4, 1.5, -5.8), 0xe0683c)

    // selection highlight
    const halo = new THREE.Mesh(new THREE.RingGeometry(0.52, 0.66, 36),
      new THREE.MeshBasicMaterial({ color: 0x35c2d6, side: THREE.DoubleSide, transparent: true, opacity: 0.9 }))
    halo.rotation.x = -Math.PI / 2; halo.visible = false
    scene.add(halo)

    const ANCHORS = {
      'TK-101': new THREE.Vector3(-5.4, 4.25, 0),
      'P-101': new THREE.Vector3(-2.3, 2.35, 0),
      'PT-101': new THREE.Vector3(1.4, 3.05, 0),
      'PT-102': new THREE.Vector3(3.6, 2.05, 0),
      'PLC-101': new THREE.Vector3(-3.4, 2.45, -5.8),
      'telemetry-gateway': new THREE.Vector3(0.4, 3.35, -5.8),
      'decoy-endpoint': new THREE.Vector3(4.4, 2.45, -5.8),
    }

    // ---- interaction -------------------------------------------------------
    const ray = new THREE.Raycaster()
    const ptr = new THREE.Vector2()
    const onClick = (e) => {
      const r = renderer.domElement.getBoundingClientRect()
      ptr.x = ((e.clientX - r.left) / r.width) * 2 - 1
      ptr.y = -((e.clientY - r.top) / r.height) * 2 + 1
      ray.setFromCamera(ptr, camera)
      const hit = ray.intersectObjects(pickable, false)[0]
      if (hit?.object?.userData?.assetId) onSelect?.(hit.object.userData.assetId)
    }
    renderer.domElement.addEventListener('click', onClick)

    // slow orbit on drag
    let drag = null, theta = Math.atan2(11, 9.5), radius = 18.2, phi = 0.40
    const down = (e) => { drag = { x: e.clientX, y: e.clientY } }
    const move = (e) => {
      if (!drag) return
      theta -= (e.clientX - drag.x) * 0.006
      phi = Math.max(0.12, Math.min(1.15, phi - (e.clientY - drag.y) * 0.004))
      drag = { x: e.clientX, y: e.clientY }
    }
    const up = () => { drag = null }
    renderer.domElement.addEventListener('pointerdown', down)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    const wheel = (e) => { e.preventDefault(); radius = Math.max(9, Math.min(30, radius + e.deltaY * 0.014)) }
    renderer.domElement.addEventListener('wheel', wheel, { passive: false })

    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight
      if (!w || !h) return
      renderer.setSize(w, h, false)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
    }
    resize()
    const ro = new ResizeObserver(resize); ro.observe(el)

    // ---- animation ----------------------------------------------------------
    let raf = 0, t0 = performance.now()
    const project = (v) => {
      const p = v.clone().project(camera)
      return { x: (p.x * 0.5 + 0.5) * 100, y: (-p.y * 0.5 + 0.5) * 100, vis: p.z < 1 }
    }

    const tick = () => {
      raf = requestAnimationFrame(tick)
      const now = performance.now()
      const dt = (now - t0) / 1000; t0 = now
      const { sample: s, truth: tr, showTruth: st, gateway: gwState,
              selected: sel } = live.current

      // A feed that disagrees but no longer drives the controller is
      // *bypassed*, not failing. Painting it the same red as a live fault
      // made a station that had been recovered under containment read as
      // broken -- so bypassed state gets its own colour.
      const altered = s && s.pt101_local_bar != null && s.pt101_forwarded_bar != null
        && Math.abs(s.pt101_forwarded_bar - s.pt101_local_bar) > 0.2
      const bypassed = altered && s.feedback_source === 'PT-102'
      const holding = (gwState?.contained_clients || []).length > 0

      camera.position.set(radius * Math.cos(phi) * Math.sin(theta), radius * Math.sin(phi) + 1.4,
                          radius * Math.cos(phi) * Math.cos(theta) - 1.6)
      camera.lookAt(-0.3, 1.4, -2.0)

      if (s) {
        const lvl = Math.max(0.08, (s.tank_level_pct ?? 70) / 100) * 3.1
        water.scale.y = lvl
        water.position.y = lvl / 2 + 0.18
        fan.rotation.x += dt * (s.pump_rpm ?? 0) * 0.0021
        const load = Math.min(1, (s.pump_output_pct ?? 0) / 100)
        casing.material.color.setHSL(0.42 - 0.42 * load, 0.5, 0.33 + 0.08 * load)
        const press = st && tr ? tr.true_pressure_bar : (s.pt102_bar ?? s.pt101_forwarded_bar ?? 0)
        const over = Math.min(1, Math.max(0, (press - 4) / 5))
        discharge.material.color.setRGB(0.36 + over * 0.55, 0.45 - over * 0.2, 0.54 - over * 0.3)
        linkPT101.material.color.set(
          bypassed ? 0x8d6cc4 : (altered ? 0xc9483f
                   : (s.decoy_active ? 0x7a6a4a : 0xd3a23a)))
        pt101.head.material.color.set(
          bypassed ? 0x7a62a8 : (altered ? 0xbf4238 : 0xd3a23a))
        gw.material.color.set(
          holding || bypassed ? 0x5c4487 : (altered ? 0x6e2f33 : 0x44405f))
        linkPT102.material.color.set(s.feedback_source === 'PT-102' ? 0x2ecc87 : 0x3f9fd0)
        linkDecoy.visible = !!s.decoy_active
        decoyBox.material.color.set(s.decoy_active ? 0xe0683c : 0x4a3024)
        plc.material.color.set(s.controller_mode === 'safe_state' ? 0x8a5a2a : 0x3d5064)
      }

      if (sel && ANCHORS[sel]) {
        halo.visible = true
        const a = ANCHORS[sel]
        halo.position.set(a.x, 0.04, a.z)
        halo.material.opacity = 0.55 + 0.35 * Math.sin(now * 0.004)
      } else halo.visible = false

      renderer.render(scene, camera)

      if (s) {
        const ref = s.pt102_bar, fwd = s.pt101_forwarded_bar, loc = s.pt101_local_bar
        const disagree = ref != null && fwd != null && Math.abs(fwd - ref) > 0.2
        const next = [
          { id: 'TK-101', label: 'TK-101', value: `${fmt(s.tank_level_pct, 0)} %`, cls: '' },
          { id: 'P-101', label: 'P-101', value: `${fmt(s.pump_output_pct, 0)} % · ${fmt(s.pump_rpm, 0)} rpm`,
            cls: (s.pump_output_pct ?? 0) > 88 ? 'warn' : '' },
          { id: 'PT-101', label: 'PT-101 fwd',
            value: bypassed ? `${fmt(fwd)} bar · bypassed` : `${fmt(fwd)} bar`,
            cls: bypassed ? 'contain' : (disagree ? 'bad' : 'ok') },
          { id: 'PT-102', label: 'PT-102 ref',
            value: ref == null ? 'no signal' : `${fmt(ref)} bar`,
            cls: ref == null ? 'bad' : (s.pt102_quality !== 'good' ? 'warn' : 'ok') },
          { id: 'PLC-101', label: 'PLC-101',
            value: s.controller_mode === 'safe_state' ? 'SAFE STATE' : `fb ${s.feedback_source}`,
            cls: s.controller_mode === 'safe_state' ? 'warn' : '' },
          { id: 'telemetry-gateway', label: 'Gateway',
            value: holding ? 'containing'
                 : (bypassed ? 'altered · off control'
                 : (altered ? 'value altered' : 'passthrough')),
            cls: holding || bypassed ? 'contain' : (altered ? 'bad' : '') },
        ]
        if (s.decoy_active) next.push({ id: 'decoy-endpoint', label: 'Decoy', value: 'ACTIVE', cls: 'warn' })
        setTags(next.map((n) => ({ ...n, ...project(ANCHORS[n.id]) })))
      }
    }
    tick()

    return () => {
      cancelAnimationFrame(raf); ro.disconnect()
      renderer.domElement.removeEventListener('click', onClick)
      renderer.domElement.removeEventListener('pointerdown', down)
      renderer.domElement.removeEventListener('wheel', wheel)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      renderer.dispose()
      if (renderer.domElement.parentNode === el) el.removeChild(renderer.domElement)
    }
  }, [onSelect])

  return (
    <div className="scene" ref={mount}>
      <div className="hud">
        {tags.filter((t) => t.vis).map((t) => (
          <div key={t.id} className={`tag ${t.cls}`} style={{ left: `${t.x}%`, top: `${t.y}%` }}>
            <span className="l">{t.label}</span>{t.value}
          </div>
        ))}
      </div>
      <div className="tools">
        <button onClick={onToggleMaximize}
                title={maximized ? 'Exit full view (Esc)' : 'Expand the station view'}>
          {maximized ? '⤡  exit' : '⤢  expand'}
        </button>
      </div>
      <div className="hint">drag to orbit · scroll to zoom · click an asset to inspect</div>
    </div>
  )
}
