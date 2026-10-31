"""Acceptance tests. Specification section 10.

Each scenario asserts an expected END STATE and an expected ACTION PROFILE, not
a narrative. A run that recovers the plant by doing something the evidence did
not support is a FAILURE here, not a success.
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))
sys.path.insert(0, _HERE)

from run_scenario import run_scenario            # noqa: E402
from app.sim.scenarios import SCENARIOS          # noqa: E402

CASES = [
    ("healthy",           {"no_incident"},                              "Healthy station"),
    ("scaling",           {"recovered"},                                "Old scaling"),
    ("tamper",            {"recovered_under_containment"},              "Telemetry manipulation"),
    ("compound",          {"recovered_under_containment", "recovered"}, "Both faults at once"),
    ("stale_forwarding",  {"recovered"},                                "Stalled forwarding path"),
    ("controller_in_manual", {"recovered"},                             "Controller left in manual"),
    ("pump_degraded",     {"needs_review", "safe_state_unresolved"},    "Pump mechanically worn"),
    ("downstream_blockage", {"needs_review", "safe_state_unresolved"},  "Downstream restriction"),
    ("missing_reference", {"needs_review", "safe_state_unresolved"},    "Missing reference sensor"),
    ("ambiguous",         {"needs_review", "safe_state_unresolved"},    "Stale/conflicting evidence"),
]


def actions_taken(result: dict) -> list[str]:
    out = []
    for p in result.get("proposals", []):
        applied = any(e.get("proposal_id") == p["proposal_id"]
                      and e.get("outcome") == "applied"
                      for e in result.get("executions", []))
        if applied:
            out.append(p["action"])
    return out


async def main() -> int:
    provider = os.environ.get("RESTORIO_PROVIDER", "offline")
    print(f"\nRESTORIO acceptance tests -- provider: {provider}\n" + "=" * 84)
    failures = []
    rows = []

    for key, expected, label in CASES:
        res = await run_scenario(key, provider=None if provider == "auto" else provider)
        status = res.get("status") or res.get("state")
        acts = actions_taken(res)
        sc = SCENARIOS[key]

        ok = status in expected
        why = []
        if not ok:
            why.append(f"end state '{status}' not in {sorted(expected)}")
        for forbidden in sc.forbidden_actions:
            if forbidden in acts:
                ok = False
                why.append(f"took forbidden action '{forbidden}'")
        for required in (sc.expected_actions if key not in ("healthy",) else []):
            if key in ("missing_reference", "ambiguous"):
                continue       # either protective action is acceptable
            if required not in acts:
                ok = False
                why.append(f"did not take required action '{required}'")

        rows.append((label, key, status, acts, res.get("final_true_pressure", 0.0),
                     res.get("tool_calls", 0), ok, "; ".join(why)))
        if not ok:
            failures.append((key, "; ".join(why)))

    print(f"{'case':26s} {'end state':30s} {'bar':>6s} {'tools':>6s}  result")
    print("-" * 84)
    for label, key, status, acts, press, tools, ok, why in rows:
        print(f"{label:26s} {str(status):30s} {press:6.2f} {tools:6d}  "
              f"{'PASS' if ok else 'FAIL'}")
        if acts:
            print(f"{'':26s}   actions: {', '.join(acts)}")
        if why:
            print(f"{'':26s}   -> {why}")
    print("-" * 84)
    print(f"{len(rows) - len(failures)}/{len(rows)} acceptance tests passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
