"""Reasoning suite — answering "what is the language model actually for?"

Our deterministic reasoner matches the model on every other scenario. That is
an honest result, and on its own it invites a fair question: if a few hundred
lines of hand-written logic score the same, why is a model in the loop?

These three cases answer it. In each, every STRUCTURED field a rule keys on
looks correct — the asset registry agrees with the configuration, the quality
flags read good. The fact that resolves the incident is written in prose, in a
maintenance record, in the way people actually write them: a loaner fitted off
the shelf, a re-range done outside the MOC route, a calibration exception
raised and ignored.

A keyed decision procedure cannot generalise to these. Reading can.

    python3 scripts/reasoning.py                    # whatever .env says
    python3 scripts/reasoning.py offline            # the rules baseline
    python3 scripts/reasoning.py --compare          # both, side by side
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

SEEDS = [7, 23]
KEYS = [k for k, s in SCENARIOS.items() if s.reasoning]


def applied(res: dict) -> list[str]:
    return [p["action"] for p in res.get("proposals", [])
            if any(e.get("proposal_id") == p["proposal_id"]
                   and e.get("outcome") == "applied"
                   for e in res.get("executions", []))]


def grade(res: dict, key: str) -> dict:
    sc = SCENARIOS[key]
    status = res.get("status") or res.get("state")
    acts = applied(res)
    forbidden = [a for a in acts if a in sc.forbidden_actions]
    needed = [a for a in sc.expected_actions if a not in acts]
    ok = status in sc.expected_end_states and not forbidden and not needed

    # Did it reach the answer for the RIGHT REASON?
    #
    # Deliberately strict: the record has to appear in what the agent actually
    # SAID, not merely in the bulk list of everything it retrieved. The
    # deterministic reasoner cites every document it touched, so accepting the
    # citation list would score it correct for reasoning it never did.
    blob = " ".join([res.get("diagnosis") or "",
                     " ".join(res.get("narrative") or [])])
    reasoned = bool(sc.required_evidence) and all(e in blob for e in sc.required_evidence)

    press = res.get("final_true_pressure", 0.0) or 0.0
    return {"status": status, "actions": acts, "ok": ok, "reasoned": reasoned,
            "forbidden": forbidden, "missing": needed,
            "pressure": press, "deviation": abs(press - 4.0),
            "tool_calls": res.get("tool_calls", 0)}


async def run(provider: str) -> dict:
    settings.autonomy = "full"
    rows = {}
    for key in KEYS:
        rows[key] = []
        for seed in SEEDS:
            res = await run_scenario(key, provider=provider, seed=seed)
            rows[key].append(grade(res, key))
    return rows


def render(label: str, rows: dict) -> tuple[int, int, int]:
    ok = reasoned = total = 0
    print(f"\n--- {label} ---")
    for key, runs in rows.items():
        sc = SCENARIOS[key]
        print(f"\n  {sc.title}")
        for seed, r in zip(SEEDS, runs):
            total += 1
            ok += r["ok"]
            reasoned += r["reasoned"]
            why = ""
            if r["forbidden"]:
                why = f"  <- took forbidden {', '.join(r['forbidden'])}"
            elif r["missing"]:
                why = f"  <- never did {', '.join(r['missing'])}"
            print(f"    seed {seed:3d}  {str(r['status']):28s} "
                  f"{r['pressure']:5.2f} bar  "
                  f"{'PASS' if r['ok'] else 'FAIL'}"
                  f"{'  [right reason]' if r['reasoned'] else ''}{why}")
    return ok, reasoned, total


async def main() -> int:
    args = [a for a in sys.argv[1:]]
    compare = "--compare" in args
    args = [a for a in args if not a.startswith("--")]

    print("\nREASONING SUITE — cases where the answer is only in the prose")
    print("=" * 86)
    print("In all three, the asset registry agrees with the configuration and")
    print("every quality flag reads good. Only a maintenance record disagrees.")

    if compare:
        results = {}
        paid = settings.resolved_provider()
        if paid not in PAID:
            paid = None
        for prov in [p for p in ("offline", paid) if p]:
            if prov == "offline" and "offline" in results:
                continue
            results[prov] = await run(prov)
        summary = {}
        for prov, rows in results.items():
            name = "rules baseline (offline)" if prov == "offline" else f"agent ({prov})"
            summary[name] = render(name, rows)

        print("\n" + "=" * 86)
        print(f"{'strategy':34s}{'correct':>12s}{'right reason':>16s}")
        print("-" * 86)
        for name, (ok, reasoned, total) in summary.items():
            print(f"{name:34s}{f'{ok}/{total}':>12s}{f'{reasoned}/{total}':>16s}")
        print("=" * 86)
        print("A rule can only key on fields. These cases put the answer in a")
        print("sentence, which is where the model earns its place.\n")
        return 0

    prov = pick_provider(args[0] if args else None)
    rows = await run(prov)
    ok, reasoned, total = render(f"provider: {prov}", rows)
    print(f"\n{ok}/{total} correct · {reasoned}/{total} for the right reason\n")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
