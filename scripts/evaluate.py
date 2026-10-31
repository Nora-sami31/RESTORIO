"""Evaluation harness. Specification section 10.

Runs every scenario across several noise seeds for each strategy and reports
observed counts -- not invented accuracy figures. The metrics are the ones the
project specification names:

  successful recovery, incorrect interventions, false cyber classifications,
  time to verified recovery, pressure deviation, model/tool cost,
  containment failures.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))
sys.path.insert(0, _HERE)

from app.config import settings                        # noqa: E402
from app.sim.scenarios import SCENARIOS                # noqa: E402
from baselines import run_restore_latest               # noqa: E402
from run_scenario import run_scenario                  # noqa: E402

SEEDS = [7, 23, 101, 404, 977]
SETPOINT = 4.0


def applied_actions(res: dict) -> list[str]:
    if "actions" in res:
        return res["actions"]
    out = []
    for p in res.get("proposals", []):
        if any(e.get("proposal_id") == p["proposal_id"] and e.get("outcome") == "applied"
               for e in res.get("executions", [])):
            out.append(p["action"])
    return out


def score(res: dict, key: str) -> dict:
    sc = SCENARIOS[key]
    status = res.get("status") or res.get("state") or "unknown"
    acts = applied_actions(res)
    truth_causes = set(sc.true_causes)

    recovered = status in ("recovered", "recovered_under_containment")
    expected_terminal = (
        status in ("recovered", "recovered_under_containment")
        if key in ("scaling", "tamper", "compound")
        else status in ("needs_review", "safe_state_unresolved", "no_incident")
    )
    if key == "healthy":
        expected_terminal = status == "no_incident"

    # An "incorrect intervention" is a state-changing action the evidence did
    # not support in this scenario.
    incorrect = [a for a in acts if a in sc.forbidden_actions]
    if key == "healthy" and acts:
        incorrect = acts

    # A "false cyber classification" is containment where no manipulation exists.
    false_cyber = ("contain_client" in acts
                   and "gateway_telemetry_manipulation" not in truth_causes)

    # Containment failure: manipulation present, containment claimed, but the
    # isolation check did not pass.
    containment_failure = False
    for v in res.get("verifications", []):
        for c in v.get("checks", []):
            if c.get("name") == "containment_holds" and not c.get("passed"):
                containment_failure = True

    press = res.get("final_true_pressure", 0.0) or 0.0
    return {
        "scenario": key, "status": status, "actions": acts,
        "correct_outcome": expected_terminal and not incorrect and not false_cyber,
        "recovered": recovered,
        "incorrect_interventions": len(incorrect),
        "false_cyber": bool(false_cyber),
        "containment_failure": containment_failure,
        "pressure_deviation": abs(press - SETPOINT),
        "final_true_pressure": press,
        "time_s": res.get("duration_s", 0.0),
        "tool_calls": res.get("tool_calls", 0),
        "llm_calls": (res.get("usage") or {}).get("llm_calls", 0),
        "tokens": ((res.get("usage") or {}).get("input_tokens", 0)
                   + (res.get("usage") or {}).get("output_tokens", 0)),
    }


async def run_strategy(strategy: str, provider: str | None, seeds: list[int],
                       scenarios: list[str]) -> list[dict]:
    rows = []
    for key in scenarios:
        for seed in seeds:
            t0 = time.time()
            if strategy == "restore_latest":
                res = await run_restore_latest(key, seed=seed)
            else:
                res = await run_scenario(key, provider=provider, seed=seed)
            row = score(res, key)
            row["strategy"] = strategy
            row["seed"] = seed
            row["wall_s"] = round(time.time() - t0, 2)
            rows.append(row)
            print(f"  {strategy:15s} {key:18s} seed {seed:4d} -> "
                  f"{row['status']:28s} {'OK ' if row['correct_outcome'] else 'BAD'} "
                  f"({row['final_true_pressure']:.2f} bar)")
    return rows


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {}
    recovered = [r for r in rows if r["recovered"]]
    times = [r["time_s"] for r in recovered if r["time_s"]]
    return {
        "runs": n,
        "correct_outcomes": sum(r["correct_outcome"] for r in rows),
        "verified_recoveries": len(recovered),
        "incorrect_interventions": sum(r["incorrect_interventions"] for r in rows),
        "false_cyber_classifications": sum(r["false_cyber"] for r in rows),
        "containment_failures": sum(r["containment_failure"] for r in rows),
        "mean_pressure_deviation_bar": round(
            statistics.fmean(r["pressure_deviation"] for r in rows), 3),
        "median_time_to_verified_recovery_s": round(statistics.median(times), 2) if times else None,
        "mean_tool_calls": round(statistics.fmean(r["tool_calls"] for r in rows), 1),
        "mean_llm_calls": round(statistics.fmean(r["llm_calls"] for r in rows), 1),
        "total_tokens": sum(r["tokens"] for r in rows),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="restore_latest,rules",
                    help="comma list: restore_latest, rules, agent")
    ap.add_argument("--provider", default=None,
                    help="provider for the 'agent' strategy (anthropic/openai/ollama)")
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--out", default="docs/evaluation_results.json")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    scenarios = [s for s in args.scenarios.split(",") if s.strip()]
    settings.autonomy = "full"          # the harness approves nothing by hand

    all_rows, summary = [], {}
    for strategy in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        print(f"\n--- {strategy} ---")
        provider = "offline" if strategy == "rules" else (args.provider or "anthropic")
        rows = await run_strategy(strategy, provider, seeds, scenarios)
        all_rows += rows
        summary[strategy] = summarise(rows)

    print("\n" + "=" * 100)
    print(f"{'metric':42s}" + "".join(f"{s:>19s}" for s in summary))
    print("-" * 100)
    labels = [
        ("runs", "runs"),
        ("correct_outcomes", "correct outcomes"),
        ("verified_recoveries", "verified recoveries"),
        ("incorrect_interventions", "incorrect interventions"),
        ("false_cyber_classifications", "false cyber classifications"),
        ("containment_failures", "containment failures"),
        ("mean_pressure_deviation_bar", "mean |pressure - setpoint| (bar)"),
        ("median_time_to_verified_recovery_s", "median time to verified recovery (s)"),
        ("mean_tool_calls", "mean tool calls"),
        ("mean_llm_calls", "mean model calls"),
        ("total_tokens", "total tokens"),
    ]
    for k, lab in labels:
        print(f"{lab:42s}" + "".join(f"{str(summary[s].get(k, '—')):>19s}" for s in summary))
    print("=" * 100)
    print("Counts observed on a simulated station. Not a claim about real plants.\n")

    out = os.path.join(os.path.dirname(_HERE), args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"generated_at": time.time(), "seeds": seeds,
                   "scenarios": scenarios, "summary": summary, "rows": all_rows},
                  f, indent=2)
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
