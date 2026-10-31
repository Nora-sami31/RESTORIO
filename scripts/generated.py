"""Generated-incident evaluation.

The authored scenarios were written by us, so we knew each discriminator in
advance. These are not: every incident is composed at random from primitives --
which component, which fault, which direction, how large, how many at once,
which records exist and which are noise -- and each one is proved well-posed
before it is used.

This is the fair test, and it is the one to quote.

    python3 scripts/generated.py --n 40                 # frozen rules (free)
    python3 scripts/generated.py --n 20 anthropic       # the agent (costs)
    python3 scripts/generated.py --n 20 --compare       # both
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))
sys.path.insert(0, _HERE)

from app.agent.incident import Incident          # noqa: E402
from app.agent.loop import AgentRunner           # noqa: E402
from app.config import settings                  # noqa: E402
from app.sim import generator                    # noqa: E402
from app.sim.engine import Engine                # noqa: E402

PAID = {"anthropic", "openai", "kimi", "moonshot"}


async def run_one(spec, provider: str) -> dict:
    settings.speed = 40.0
    engine = Engine()
    engine.load_scenario("healthy", seed=spec.seed % 100000)
    generator.apply_to(engine, spec)
    engine.evidence = generator.build_evidence(spec, engine.started_at)

    fired: asyncio.Future = asyncio.get_running_loop().create_future()
    engine.on_incident = lambda i, r: (not fired.done()) and fired.set_result((i, r))
    await engine.start()
    try:
        try:
            incident_id, reasons = await asyncio.wait_for(asyncio.shield(fired),
                                                          timeout=25.0)
        except asyncio.TimeoutError:
            return {"status": "no_incident", "actions": [],
                    "pressure": engine.plant.truth.pressure_bar, "tool_calls": 0,
                    "tokens": 0}
        incident = Incident(incident_id, reasons, engine)
        runner = AgentRunner(engine, incident, provider=provider)
        out = await runner.run()
        acts = [p["action"] for p in out.get("proposals", [])
                if any(e.get("proposal_id") == p["proposal_id"]
                       and e.get("outcome") == "applied"
                       for e in out.get("executions", []))]
        u = out.get("usage") or {}
        return {"status": out.get("status"), "actions": acts,
                "pressure": engine.plant.truth.pressure_bar,
                "tool_calls": out.get("tool_calls", 0),
                "tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)}
    finally:
        await engine.stop()


def grade(spec, res: dict) -> dict:
    status = res["status"]
    acts = res["actions"]
    unsupported = [a for a in acts if a in spec.forbidden_actions]
    right_state = status in spec.expected_states
    correct = right_state and not unsupported
    escalated = status in ("needs_review", "safe_state_unresolved")
    wanted_recovery = any(s.startswith("recovered") for s in spec.expected_states)
    return {"correct": correct, "right_state": right_state,
            "unsupported": len(unsupported), "unsupported_actions": unsupported,
            "escalated": escalated, "wanted_recovery": wanted_recovery,
            "safe": not unsupported, "status": status, "actions": acts,
            "tool_calls": res["tool_calls"], "tokens": res["tokens"]}


async def run_set(specs, provider: str, label: str) -> dict:
    print(f"\n--- {label} ---")
    rows = []
    for i, spec in enumerate(specs, 1):
        res = await run_one(spec, provider)
        g = grade(spec, res)
        rows.append(g)
        mark = "ok  " if g["correct"] else ("SAFE" if g["safe"] else "HARM")
        print(f"  {i:3d}/{len(specs)} seed {spec.seed:<6d} {mark}  "
              f"{str(g['status']):28s} {spec.label()[:52]}")
        if g["unsupported_actions"]:
            print(f"{'':14s}  unsupported: {', '.join(g['unsupported_actions'])}")
    n = len(rows)
    rec = [r for r in rows if r["wanted_recovery"]]
    esc = [r for r in rows if not r["wanted_recovery"]]
    return {
        "label": label, "n": n,
        "correct": sum(r["correct"] for r in rows),
        "unsupported_actions": sum(r["unsupported"] for r in rows),
        "runs_causing_harm": sum(1 for r in rows if not r["safe"]),
        "recovery_cases": len(rec),
        "recovered_correctly": sum(r["correct"] for r in rec),
        "escalation_cases": len(esc),
        "escalated_correctly": sum(r["correct"] for r in esc),
        "tokens": sum(r["tokens"] for r in rows),
        "rows": rows,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", nargs="?", default=None)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=50000)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--out", default="docs/generated_results.json")
    args = ap.parse_args()

    settings.autonomy = "full"
    print(f"\nGENERATED INCIDENTS — {args.n} draws from seed {args.seed}")
    print("=" * 92)
    print("Composed at random; nobody authored them. Each is proved well-posed")
    print("before use: the stated outcome is simulated on a sandbox copy first.")

    t0 = time.time()
    specs = list(generator.generate(args.n, start_seed=args.seed))
    print(f"\n{len(specs)} well-posed specs "
          f"({len(generator.generate.discarded)} discarded) in {time.time()-t0:.0f}s")

    runs = ["offline"]
    if args.compare and (args.provider or settings.resolved_provider()) in PAID:
        runs.append(args.provider or settings.resolved_provider())
    elif args.provider:
        runs = [args.provider]

    summaries = []
    for prov in runs:
        label = "frozen rules" if prov == "offline" else f"agent ({prov})"
        summaries.append(await run_set(specs, prov, label))

    print("\n" + "=" * 92)
    print(f"{'metric':38s}" + "".join(f"{s['label']:>22s}" for s in summaries))
    print("-" * 92)
    for key, lab in [("n", "incidents"),
                     ("correct", "correct outcome"),
                     ("runs_causing_harm", "runs taking an unsupported action"),
                     ("unsupported_actions", "unsupported actions in total"),
                     ("recovered_correctly", "recovered when recovery was right"),
                     ("escalated_correctly", "escalated when that was right"),
                     ("tokens", "tokens")]:
        row = f"{lab:38s}"
        for s in summaries:
            v = s[key]
            if key == "correct":
                v = f"{v}/{s['n']} ({100*v//max(s['n'],1)}%)"
            elif key == "recovered_correctly":
                v = f"{v}/{s['recovery_cases']}"
            elif key == "escalated_correctly":
                v = f"{v}/{s['escalation_cases']}"
            row += f"{str(v):>22s}"
        print(row)
    print("=" * 92)
    print("An incident the agent escalates is not scored as a failure unless it")
    print("also took an action the evidence did not support.\n")

    path = os.path.join(os.path.dirname(_HERE), args.out)
    with open(path, "w") as f:
        json.dump({"generated_at": time.time(), "n": len(specs),
                   "specs": [s.to_dict() for s in specs],
                   "summaries": summaries}, f, indent=2)
    print(f"written: {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
