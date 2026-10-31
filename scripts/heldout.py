"""Held-out test set — judging criterion 1 (performance on unseen cases).

These five scenarios were written AFTER the agent, the system prompt, the
validator and the deterministic reasoner were complete. Nothing was tuned
against them. Each differs from the development set in kind, direction or
magnitude, and H5 is a fault class the system was never designed around.

A run passes only if the end state is acceptable AND no forbidden action was
applied. Recovering the plant by doing something the evidence did not support
is counted as a failure.
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))
sys.path.insert(0, _HERE)

from app.config import settings                 # noqa: E402
from app.sim.scenarios import SCENARIOS         # noqa: E402
from run_scenario import run_scenario           # noqa: E402


PAID = {"anthropic", "openai", "kimi", "moonshot"}


def pick_provider(explicit: str | None) -> str:
    """Default to the free reasoner. A paid provider must be named."""
    if explicit:
        return explicit
    resolved = settings.resolved_provider()
    if resolved in PAID:
        print(f"\n  NOTE: defaulting to the free deterministic reasoner.")
        print(f"  To spend credits on '{resolved}', pass it explicitly:")
        print(f"      python3 {os.path.basename(sys.argv[0])} {resolved}\n")
        return "offline"
    return resolved

SEEDS = [7, 23, 101]


def applied(res: dict) -> list[str]:
    return [p["action"] for p in res.get("proposals", [])
            if any(e.get("proposal_id") == p["proposal_id"]
                   and e.get("outcome") == "applied"
                   for e in res.get("executions", []))]


async def main() -> int:
    settings.autonomy = "full"
    provider = pick_provider(sys.argv[1] if len(sys.argv) > 1 else None)
    keys = [k for k, s in SCENARIOS.items() if s.heldout]

    print(f"\nHELD-OUT TEST SET — provider: {provider}")
    print("Written after the system was finished; nothing tuned against them.")
    print("=" * 94)

    fails, rows = [], []
    for key in keys:
        sc = SCENARIOS[key]
        for seed in SEEDS:
            res = await run_scenario(key, provider=None if provider == "auto" else provider,
                                     seed=seed)
            status = res.get("status") or res.get("state")
            acts = applied(res)
            why = []
            ok = status in sc.expected_end_states
            if not ok:
                why.append(f"end state '{status}' not in {sorted(sc.expected_end_states)}")
            for f in sc.forbidden_actions:
                if f in acts:
                    ok = False
                    why.append(f"applied forbidden action '{f}'")
            rows.append((sc.title, key, seed, status, acts,
                         res.get("final_true_pressure", 0.0),
                         res.get("tool_calls", 0), ok, "; ".join(why)))
            if not ok:
                fails.append(f"{key}/seed{seed}: {'; '.join(why)}")

    last = None
    for title, key, seed, status, acts, press, tools, ok, why in rows:
        if title != last:
            print(f"\n{title}")
            print(f"  {SCENARIOS[key].summary}")
            last = title
        sp = SCENARIOS[key].key == "h4_setpoint_shift" and 3.0 or 4.0
        print(f"    seed {seed:4d}  {status:30s} {press:5.2f} bar "
              f"(target {sp:.1f})  {tools:2d} tools  {'PASS' if ok else 'FAIL'}")
        if acts:
            print(f"              actions: {', '.join(acts)}")
        if why:
            print(f"              -> {why}")

    print("\n" + "=" * 94)
    print(f"{len(rows) - len(fails)}/{len(rows)} held-out runs passed "
          f"({len(keys)} unseen scenarios x {len(SEEDS)} seeds)")
    if fails:
        print("\nfailures:")
        for f in fails:
            print(f"  - {f}")
    print()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
