"""Comparison baselines. Specification section 10.

Two strategies to measure the agent against, on identical starting conditions:

  restore_latest -- the common operational reflex: when the station misbehaves,
                    restore the most recent backup image. It does not
                    investigate, and it has no validator.
  rules          -- the deterministic reasoner in app/agent/offline.py. A
                    competent hand-written decision procedure driving the same
                    tools through the same loop, with the same guardrails.

The agent is `RESTORIO_PROVIDER=anthropic` (or openai/ollama).
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))

from app.config import settings                        # noqa: E402
from app.sim.control import await_sim_seconds, score_live_samples  # noqa: E402
from app.sim.engine import Engine                      # noqa: E402


async def run_restore_latest(key: str, seed: int = 7, speed: float = 40.0) -> dict:
    """Restore the latest backup image and declare the job done.

    The backup (BKP-2026-09-12) predates the transmitter replacement, so it
    carries the superseded 0-10 bar scaling. This baseline has no validator and
    no independent verification step -- it is what "just restore it" looks like.
    """
    settings.speed = speed
    engine = Engine()
    engine.load_scenario(key, seed=seed)
    await engine.start()
    try:
        fired = asyncio.get_running_loop().create_future()
        engine.on_incident = lambda i, r: (not fired.done()) and fired.set_result((i, r))
        try:
            await asyncio.wait_for(asyncio.shield(fired), timeout=20.0)
        except asyncio.TimeoutError:
            return {"strategy": "restore_latest", "scenario_key": key,
                    "status": "no_incident", "detected": False, "actions": [],
                    "final_true_pressure": engine.plant.truth.pressure_bar,
                    "tool_calls": 0, "llm_calls": 0}

        # The whole strategy: push the backup's configuration back in.
        engine.plant.config.pt101_range_min = 0.0
        engine.plant.config.pt101_range_max = 10.0     # what the backup contains
        engine.plant.reset_integral()
        engine.plant.bump_revision()
        engine.add_marker("applied", "restore_latest: backup configuration reapplied")

        await await_sim_seconds(engine, settings.settle_seconds * 2.6)
        res = score_live_samples(engine, settings.settle_seconds,
                                 contained_clients=sorted(engine.gateway.contained) or None)
        return {
            "strategy": "restore_latest", "scenario_key": key, "detected": True,
            "status": "recovered" if res["passed"] else "not_recovered",
            "verified": res["passed"], "checks": res["checks"],
            "actions": ["restore_backup"],
            "final_true_pressure": engine.plant.truth.pressure_bar,
            "tool_calls": 0, "llm_calls": 0,
        }
    finally:
        await engine.stop()
