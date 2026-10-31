"""Headless end-to-end run of one scenario. Used by tests and the evaluation harness."""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))

from app.agent.incident import Incident          # noqa: E402
from app.agent.loop import AgentRunner           # noqa: E402
from app.config import settings                  # noqa: E402
from app.sim.engine import Engine                # noqa: E402


async def run_scenario(key: str, provider: str | None = None, seed: int = 7,
                       speed: float = 40.0) -> dict:
    settings.speed = speed
    engine = Engine()
    engine.load_scenario(key, seed=seed)

    fired: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_incident(incident_id, reasons):
        if not fired.done():
            fired.set_result((incident_id, reasons))

    engine.on_incident = on_incident
    await engine.start()
    try:
        try:
            incident_id, reasons = await asyncio.wait_for(asyncio.shield(fired),
                                                          timeout=20.0)
        except asyncio.TimeoutError:
            return {"scenario_key": key, "status": "no_incident",
                    "state": "no_incident", "detected": False,
                    "truth": engine.truth_overlay(),
                    "final_true_pressure": engine.plant.truth.pressure_bar,
                    "duration_s": 0.0, "tool_calls": 0, "iterations": 0,
                    "provider": provider or settings.resolved_provider(),
                    "executions": [], "verifications": [], "hypotheses": []}

        incident = Incident(incident_id, reasons, engine)
        runner = AgentRunner(engine, incident, provider=provider)
        summary = await runner.run()
        summary["detected"] = True
        summary["truth"] = engine.truth_overlay()
        summary["final_true_pressure"] = engine.plant.truth.pressure_bar
        return summary
    finally:
        await engine.stop()


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "scaling"
    prov = sys.argv[2] if len(sys.argv) > 2 else None
    out = asyncio.run(run_scenario(key, prov))
    print("\n" + "=" * 76)
    print(f"scenario   : {key}")
    print(f"status     : {out.get('status')}   state: {out.get('state')}")
    print(f"provider   : {out.get('provider')}  iterations: {out.get('iterations')}  "
          f"tool calls: {out.get('tool_calls')}")
    print(f"true press : {out.get('final_true_pressure', 0):.2f} bar")
    print(f"diagnosis  : {out.get('diagnosis','')[:400]}")
    print(f"note       : {out.get('operator_note','')[:300]}")
    print("=" * 76)
    print("\nTIMELINE")
    for t in out.get("timeline", []):
        if t["kind"] in ("state", "hypotheses", "proposal", "test", "execution",
                         "verification", "conclusion"):
            print(f"  [{t['elapsed_s']:6.2f}s] {t['kind']:12s} {t['label'][:90]}")
