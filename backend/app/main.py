"""RESTORIO backend: FastAPI + server-sent events.

Logical boundaries (specification section 07):
  the website reads backend events; the agent calls narrow tools; the validator
  tests proposals; the executor alone changes state; the simulator advances on
  its own clock; the decoy keeps separate state.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent.brain import probe as probe_provider
from .agent.incident import Incident
from .agent.loop import AgentRunner
from .config import settings
from .events import bus
from .sim import generator as sim_generator
from .sim.engine import engine
from .sim.scenarios import SCENARIOS
from .store import audit
from .agent import foresight
from .store import forecast as risk_forecast

app = FastAPI(title="RESTORIO", version="1.0",
              description="Agentic investigation, containment and recovery for "
                          "industrial OT (simulated station).")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

STATE: dict[str, Any] = {
    "incidents": {},            # incident_id -> Incident
    "runners": {},              # incident_id -> AgentRunner
    "tasks": {},
    "auto_run": True,
}


# ---------------------------------------------------------------------------
# agent triggering
# ---------------------------------------------------------------------------
def _on_incident(incident_id: str, reasons: list[str]) -> None:
    """Detection fired. Create the incident and start the agent exactly once."""
    if incident_id in STATE["incidents"]:
        return
    incident = Incident(incident_id, reasons, engine)
    STATE["incidents"][incident_id] = incident
    if not STATE["auto_run"]:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    STATE["tasks"][incident_id] = loop.create_task(_run_agent(incident))


async def _run_agent(incident: Incident) -> None:
    runner = AgentRunner(engine, incident)
    STATE["runners"][incident.incident_id] = runner
    try:
        summary = await runner.run()
    except Exception as exc:                          # noqa: BLE001
        incident.error = repr(exc)[:400]
        bus.emit("agent_crashed", {"incident_id": incident.incident_id,
                                   "error": repr(exc)[:300]})
        if incident.status is None:
            incident.conclude("needs_review",
                              "The agent run failed unexpectedly; the station kept "
                              "running under its deterministic controller.",
                              0.0, [], f"Internal error: {repr(exc)[:200]}")
        summary = incident.summary()
    summary["provider"] = runner.provider
    audit.save_incident(summary)

    # Re-arm so the station can report the NEXT fault. Only after a genuine
    # recovery: if the agent escalated, a human owns it now and re-detecting
    # the same unchanged fault would just loop.
    if (summary.get("status") or "").startswith("recovered"):
        engine.rearm_detection("previous incident recovered")
    else:
        engine.hold_detection(
            f"incident {incident.incident_id} closed as "
            f"{summary.get('status')}; detection stays held until an engineer "
            f"acts or the scenario is reloaded")


engine.on_incident = _on_incident


@app.on_event("startup")
async def _startup() -> None:
    audit.init()
    await engine.start()
    bus.emit("server_ready", {"provider": settings.resolved_provider(),
                              "autonomy": settings.autonomy})


@app.on_event("shutdown")
async def _shutdown() -> None:
    await engine.stop()


# ---------------------------------------------------------------------------
# station
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "running": engine.running,
            "provider": settings.resolved_provider(),
            "autonomy": settings.autonomy, "speed": settings.speed,
            "scenario": engine.scenario_key, "ts": time.time()}


@app.get("/api/provider")
async def provider() -> dict[str, Any]:
    return await asyncio.to_thread(probe_provider)


@app.get("/api/state")
async def state() -> dict[str, Any]:
    s = engine.station_state()
    s["incidents"] = [i.summary() for i in STATE["incidents"].values()]
    s["settings"] = {
        "setpoint_bar": settings.setpoint_bar,
        "target_band_bar": settings.target_band_bar,
        "agreement_tol_bar": settings.agreement_tol_bar,
        "autonomy": settings.autonomy,
        "provider": settings.resolved_provider(),
        "min_confidence_to_act": settings.min_confidence_to_act,
        "overpressure_trip_bar": settings.overpressure_trip_bar,
    }
    return s


@app.get("/api/history")
async def history(limit: int = 600) -> dict[str, Any]:
    return {"samples": list(engine.samples)[-limit:], "markers": engine.markers}


@app.get("/api/truth")
async def truth() -> dict[str, Any]:
    """AUDIENCE-ONLY ground truth overlay. Never reachable by any agent tool."""
    overlay = engine.truth_overlay()
    # For a generated incident, score what the agent concluded against what the
    # draw says is correct. Escalating is the right answer on many draws, and
    # without this the website leaves an amber badge to be misread as failure.
    spec = engine.generated_spec
    if spec is not None:
        inc = next((i for i in STATE["incidents"].values()
                    if i.status is not None), None)
        if inc is not None:
            applied = [p.action for p in inc.proposals
                       if any(e.proposal_id == p.proposal_id
                              and e.outcome == "applied" for e in inc.executions)]
            overlay["verdict"] = sim_generator.grade_outcome(
                spec, inc.status, applied)
    return {"audience_only": True,
            "warning": "Ground truth. Excluded from every agent tool by design.",
            **overlay}


@app.get("/api/telemetry")
async def telemetry(window_s: float = 30.0) -> dict[str, Any]:
    return engine.read_telemetry(window_s)


@app.get("/api/asset/{asset_id}")
async def asset(asset_id: str) -> dict[str, Any]:
    out = engine.read_asset(asset_id)
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out


@app.get("/api/evidence")
async def evidence(query: str = "", kind: str | None = None,
                   asset_id: str | None = None) -> dict[str, Any]:
    items = engine.search_evidence(query=query, kinds=[kind] if kind else None,
                                   asset_id=asset_id, limit=60)
    return {"count": len(items), "results": items}


@app.get("/api/logs")
async def logs(source: str | None = None, level: str | None = None,
               contains: str | None = None, window_s: float | None = None,
               limit: int = 60) -> dict[str, Any]:
    return engine.read_logs(source=source, level=level, contains=contains,
                            window_s=window_s, limit=limit)


@app.get("/api/metrics")
async def metrics(name: str | None = None, window_s: float = 60.0) -> dict[str, Any]:
    return engine.read_metrics(name=name, window_s=window_s)


@app.get("/api/gateway")
async def gateway() -> dict[str, Any]:
    return engine.gateway.status()


@app.get("/api/access-log")
async def access_log() -> dict[str, Any]:
    return {"events": [e.to_dict() for e in engine.gateway.access_log]}


# ---------------------------------------------------------------------------
# scenarios & simulation control
# ---------------------------------------------------------------------------
@app.get("/api/scenarios")
async def scenarios() -> dict[str, Any]:
    return {"scenarios": [
        {"key": k, "title": s.title, "summary": s.summary,
         "notes": s.notes, "hidden_from_agent": True}
        for k, s in SCENARIOS.items()]}


@app.post("/api/scenario/{key}")
async def load_scenario(key: str, seed: int | None = None) -> dict[str, Any]:
    if key not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario '{key}'")
    for t in STATE["tasks"].values():
        t.cancel()
    STATE["incidents"].clear()
    STATE["runners"].clear()
    STATE["tasks"].clear()
    out = engine.load_scenario(key, seed=seed)
    if not engine.running:
        await engine.start()
    return {**out, "ok": True}


@app.post("/api/generate")
async def generate_incident(seed: int | None = None,
                            recoverable: bool = True) -> dict[str, Any]:
    """Draw a random, well-posed incident nobody authored."""
    for t in STATE["tasks"].values():
        t.cancel()
    STATE["incidents"].clear()
    STATE["runners"].clear()
    STATE["tasks"].clear()
    out = await asyncio.to_thread(engine.load_generated, seed, recoverable)
    if not engine.running:
        await engine.start()
    return {**out, "ok": True}


@app.post("/api/sim/{action}")
async def sim_control(action: str, speed: float | None = None) -> dict[str, Any]:
    if action == "start":
        await engine.start()
    elif action == "stop":
        await engine.stop()
    elif action == "speed" and speed:
        settings.speed = max(0.1, min(60.0, float(speed)))
    else:
        raise HTTPException(400, "action must be start, stop or speed")
    return {"ok": True, "running": engine.running, "speed": settings.speed}


@app.post("/api/autonomy/{mode}")
async def autonomy(mode: str) -> dict[str, Any]:
    if mode not in ("supervised", "guarded", "full"):
        raise HTTPException(400, "mode must be supervised, guarded or full")
    settings.autonomy = mode
    bus.emit("autonomy_changed", {"autonomy": mode})
    return {"ok": True, "autonomy": mode}


# ---------------------------------------------------------------------------
# incidents
# ---------------------------------------------------------------------------
@app.get("/api/incidents")
async def incidents() -> dict[str, Any]:
    live = [i.summary() for i in STATE["incidents"].values()]
    return {"live": live, "history": audit.list_incidents()}


# The agentic forecast costs a provider call, so it is produced on request and
# held here rather than recomputed on every dashboard poll.
_FORECAST: dict[str, Any] = {"result": None}


@app.get("/api/forecast")
async def forecast() -> dict[str, Any]:
    """The statistics, free, plus whichever agentic forecast was last produced.

    Read-only and advisory. A failure here must never take the dashboard with
    it, so the whole thing degrades to "unavailable" rather than raising.
    """
    try:
        rows = audit.occurrences()
        base = risk_forecast.forecast(rows, engine, time.time())
    except Exception as exc:                       # pragma: no cover - defensive
        base = {"history": {"available": False, "reason": str(exc),
                            "incidents": 0, "classes": [], "headline": None},
                "live": {"available": False, "reason": "history unavailable"}}
    return base | {"agent": _FORECAST["result"]}


@app.post("/api/forecast/run")
async def forecast_run() -> dict[str, Any]:
    """Produce an agentic forecast: the model reads the history and concludes.

    In LLM mode this spends a provider call. Offline it falls back to the rules
    baseline, which is reported as rules rather than as reasoning.
    """
    rows = audit.occurrences()
    if settings.provider == "offline":
        result = await asyncio.to_thread(
            foresight.offline_forecast, engine, rows, time.time())
    else:
        agent = foresight.ForecastAgent(engine, rows, time.time())
        try:
            result = await asyncio.to_thread(agent.run)
        except Exception as exc:                   # pragma: no cover - defensive
            result = {"available": False, "agentic": True,
                      "provider": settings.provider, "reason": str(exc),
                      "tool_trail": agent.trail, "usage": agent.usage}
    _FORECAST["result"] = result
    bus.emit("forecast_ready", {"available": bool(result.get("available")),
                                "agentic": bool(result.get("agentic"))})
    return result


@app.get("/api/incident/{incident_id}")
async def incident(incident_id: str) -> dict[str, Any]:
    inc = STATE["incidents"].get(incident_id)
    if inc:
        return inc.summary()
    rec = audit.get_incident(incident_id)
    if rec:
        return rec
    raise HTTPException(404, "unknown incident")


@app.post("/api/incident/{incident_id}/approve")
async def approve(incident_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    inc = STATE["incidents"].get(incident_id)
    if not inc:
        raise HTTPException(404, "unknown incident")
    pid = (payload or {}).get("proposal_id") or (
        inc.pending_approval.proposal_id if inc.pending_approval else None)
    if not pid or not inc.grant_approval(pid):
        raise HTTPException(400, "no matching proposal is awaiting approval")
    ev = getattr(inc, "approval_event", None)
    if ev:
        ev.set()
    return {"ok": True, "proposal_id": pid}


@app.post("/api/incident/{incident_id}/deny")
async def deny(incident_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    inc = STATE["incidents"].get(incident_id)
    if not inc:
        raise HTTPException(404, "unknown incident")
    pid = (payload or {}).get("proposal_id") or (
        inc.pending_approval.proposal_id if inc.pending_approval else None)
    if not pid or not inc.deny_approval(pid, (payload or {}).get("reason", "")):
        raise HTTPException(400, "no matching proposal is awaiting approval")
    ev = getattr(inc, "approval_event", None)
    if ev:
        ev.set()
    return {"ok": True, "proposal_id": pid}


@app.post("/api/agent/run")
async def run_agent_now() -> dict[str, Any]:
    """Manually trigger the agent against the current station state."""
    from .sim.records import new_id
    iid = engine.incident_id or new_id("INC")
    engine.incident_id = iid
    engine.detection_locked = True
    if iid in STATE["incidents"]:
        raise HTTPException(400, "an incident is already open")
    inc = Incident(iid, ["Manually triggered from the operator console."], engine)
    STATE["incidents"][iid] = inc
    STATE["tasks"][iid] = asyncio.get_running_loop().create_task(_run_agent(inc))
    return {"ok": True, "incident_id": iid}


# ---------------------------------------------------------------------------
# decoy demonstration
# ---------------------------------------------------------------------------
@app.post("/api/decoy/probe")
async def decoy_probe(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a write from the controlled test client and show where it lands.

    Before containment this reaches operational state. After containment it
    reaches only the decoy -- which is the isolation claim, demonstrated live.
    """
    body = payload or {}
    client = body.get("client_id", "test-client-07")
    value = float(body.get("value", 1.75))

    # Judge this on the state the write actually TARGETS, not on the computed
    # reading. The forwarded value carries sensor noise, so comparing it would
    # report "operational state changed" on a write that went to the decoy and
    # changed nothing -- which reads as a containment failure when containment
    # in fact held.
    st0 = engine.gateway.status()
    before = engine.gateway.forwarded_value()
    result = engine.gateway.client_write(client, "pt101_forwarded_offset_bar", value,
                                         note="Operator-triggered demonstration write.")
    after = engine.gateway.forwarded_value()
    st1 = engine.gateway.status()
    changed = (st0["forwarded_offset_bar"] != st1["forwarded_offset_bar"]
               or st0["forwarded_override_bar"] != st1["forwarded_override_bar"])
    # A write that reaches operational state IS a live fault, so the
    # audience overlay has to say so. Without this it reported "no faults"
    # while the plant was being driven off setpoint by the very write the
    # operator had just triggered.
    if changed:
        faults = engine.plant.truth.active_faults
        if "gateway_telemetry_manipulation" not in faults:
            faults.append("gateway_telemetry_manipulation")

    out = {
        "client_id": client, "attempted_value": value,
        "route": result.get("route"), "accepted": result.get("accepted"),
        "operational_forwarded_before": round(before, 3),
        "operational_forwarded_after": round(after, 3),
        "operational_offset_before": st0["forwarded_offset_bar"],
        "operational_offset_after": st1["forwarded_offset_bar"],
        "operational_state_changed": changed,
        "decoy": engine.gateway.status()["decoy"],
    }
    bus.emit("decoy_probe", out)
    return out


@app.get("/api/decoy")
async def decoy() -> dict[str, Any]:
    st = engine.gateway.status()
    return {"active": st["decoy_active"], **st["decoy"],
            "contained_clients": st["contained_clients"], "routes": st["routes"]}


# ---------------------------------------------------------------------------
# event stream
# ---------------------------------------------------------------------------
@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    queue = bus.subscribe()

    async def gen():
        try:
            snapshot = {"kind": "snapshot", "ts": time.time(),
                        "payload": engine.station_state()}
            yield f"data: {json.dumps(snapshot, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(evt, default=str)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive",
        "X-Accel-Buffering": "no"})


@app.get("/api/events")
async def events(limit: int = 200) -> dict[str, Any]:
    return {"events": bus.history(limit=limit)}


# ---------------------------------------------------------------------------
# static frontend (built by `npm run build` into frontend/dist)
# ---------------------------------------------------------------------------
_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "..", "frontend", "dist")
_DIST = os.path.abspath(_DIST)

if settings.serve_frontend and os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")),
              name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(os.path.join(_DIST, "index.html"))

    @app.get("/{path:path}")
    async def spa(path: str):
        candidate = os.path.join(_DIST, path)
        if path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_DIST, "index.html"))
else:
    @app.get("/")
    async def index_dev() -> JSONResponse:
        return JSONResponse({
            "service": "RESTORIO backend",
            "frontend": "not built -- run `npm install && npm run dev` in frontend/, "
                        "or `npm run build` to have the backend serve it",
            "docs": "/docs", "stream": "/api/stream", "state": "/api/state"})
