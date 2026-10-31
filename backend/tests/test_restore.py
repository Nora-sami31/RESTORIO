"""Unit tests for the safety-critical parts.

Run with:  python3 -m pytest backend/tests -q    (or: python3 backend/tests/test_restore.py)
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.config import settings
from app.sim.control import Executor, Validator
from app.sim.engine import Engine
from app.sim.plant import ma_to_pressure, pressure_to_ma
from app.sim.records import Proposal, new_id

FAILURES: list[str] = []


def reports(fn):
    """Make a check-based test fail under pytest as well as under __main__.

    Each test records its results with check() so the whole section still
    prints, then this asserts that nothing in THIS test failed. Without it
    pytest collects these functions, runs them, sees no assertion and reports
    a pass regardless of what the checks found.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = len(FAILURES)
        fn(*args, **kwargs)
        failed = FAILURES[start:]
        assert not failed, (f"{len(failed)} check(s) failed in {fn.__name__}: "
                            + ", ".join(failed))
    return wrapper


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def settle(engine: Engine, seconds: float) -> None:
    for _ in range(int(seconds / settings.dt)):
        engine.tick()


def mk(engine, action, changes, rev=None):
    return Proposal(proposal_id=new_id("PRP"), incident_id="INC-test", action=action,
                    target="PLC-101", changes=changes, evidence_ids=["WO-2291"],
                    rationale="unit test", expected_checks=[],
                    bound_revision=rev if rev is not None else engine.plant.config_revision,
                    risk="high")


@reports
def test_current_loop_maths() -> None:
    print("\n4-20 mA conversion")
    check("6 bar on a 0-20 device is 8.8 mA", abs(pressure_to_ma(6.0) - 8.8) < 1e-6)
    check("8.8 mA read as 0-10 bar gives 3 bar",
          abs(ma_to_pressure(8.8, 0, 10) - 3.0) < 1e-6)
    check("round trip is lossless",
          abs(ma_to_pressure(pressure_to_ma(7.3), 0, 20) - 7.3) < 1e-9)
    check("signal is clamped at full span", pressure_to_ma(999.0) == 20.0)


@reports
def test_ground_truth_is_not_leaked() -> None:
    print("\nGround truth isolation")
    e = Engine(); e.load_scenario("scaling", seed=7); settle(e, 30)
    import json
    surfaces = {
        "read_telemetry": e.read_telemetry(30),
        "read_asset:PT-101": e.read_asset("PT-101"),
        "read_asset:PLC-101": e.read_asset("PLC-101"),
        "read_asset:gateway": e.read_asset("telemetry-gateway"),
        "search_evidence": e.search_evidence("scaling range"),
        "config_snapshot": e.config_snapshot(),
    }
    true_press = round(e.plant.truth.pressure_bar, 2)
    for name, payload in surfaces.items():
        blob = json.dumps(payload, default=str)
        check(f"{name} hides the scenario label", "pt101_scaling_mismatch" not in blob)
        check(f"{name} hides active_faults", "active_faults" not in blob)
        check(f"{name} hides true pressure value",
              f"{true_press}" not in blob or name == "read_telemetry")
    check("truth_overlay does expose it (audience path)",
          "pt101_scaling_mismatch" in json.dumps(e.truth_overlay()))


@reports
def test_validator_refusals() -> None:
    print("\nValidator refusals")
    e = Engine(); e.load_scenario("scaling", seed=7); settle(e, 40)
    v = Validator(e)
    check("stale revision is rejected",
          not v.validate(mk(e, "apply_config_patch", {"pt101_range_max": 20.0}, rev=3)).passed)
    check("non-whitelisted field is rejected",
          not v.validate(mk(e, "apply_config_patch", {"kp": 99.0})).passed)
    check("out-of-bounds value is rejected",
          not v.validate(mk(e, "apply_config_patch", {"pt101_range_max": 999.0})).passed)
    check("unsupported scaling value is rejected",
          not v.validate(mk(e, "apply_config_patch", {"pt101_range_max": 15.0})).passed)
    check("supported scaling value is accepted",
          v.validate(mk(e, "apply_config_patch", {"pt101_range_max": 20.0})).passed)
    check("containment without access-log evidence is rejected",
          not v.validate(mk(e, "contain_client", {"client_id": "test-client-07"})).passed)
    check("unknown action is rejected",
          not v.validate(mk(e, "reboot_everything", {})).passed)


@reports
def test_executor_requires_passing_test() -> None:
    print("\nExecutor gate")
    e = Engine(); e.load_scenario("scaling", seed=7); settle(e, 40)
    v, x = Validator(e), Executor(e)
    p = mk(e, "apply_config_patch", {"pt101_range_max": 20.0})
    check("apply without a report is refused", x.apply(p, None).outcome == "rejected")
    bad = v.validate(mk(e, "apply_config_patch", {"pt101_range_max": 15.0}))
    check("apply with a failing report is refused",
          x.apply(p, bad).outcome == "rejected")
    good = v.validate(p)
    check("apply with a passing report succeeds", x.apply(p, good).outcome == "applied")
    check("revision advanced", e.plant.config_revision == 8)


@reports
def test_failover_refused_without_reference() -> None:
    print("\nFailover prerequisites")
    e = Engine(); e.load_scenario("missing_reference", seed=7); settle(e, 40)
    r = Validator(e).validate(mk(e, "switch_feedback_source", {"feedback_source": "PT-102"}))
    check("failover to an offline reference is refused", not r.passed)
    names = [c["name"] for c in r.checks if not c["passed"]]
    check("refusal cites the freshness check", "fallback_freshness" in names, str(names))


@reports
def test_containment_isolation() -> None:
    print("\nContainment isolation")
    e = Engine(); e.load_scenario("tamper", seed=7); settle(e, 40)
    before_cfg = e.plant.config.snapshot()
    e.gateway.contain_client("test-client-07")
    for _ in range(6):
        e.gateway.client_write("test-client-07", "pt101_forwarded_offset_bar", 3.3)
    proof = e.gateway.containment_proof("test-client-07")
    check("probe is routed to the decoy", proof["probe_route"] == "decoy")
    check("operational state is untouched", not proof["reached_operational_state"])
    check("isolation holds", proof["isolation_holds"])
    check("decoy recorded the writes", e.gateway.decoy.writes_received >= 6)
    check("plant configuration unchanged", e.plant.config.snapshot() == before_cfg)


@reports
def test_invariants_on_conclude() -> None:
    print("\nIncident invariants")
    from app.agent.incident import Incident
    e = Engine(); e.load_scenario("scaling", seed=7); settle(e, 30)
    i = Incident("INC-x", ["test"], e)
    out = i.conclude("recovered", "all good", 0.99, [])
    check("recovered without verification is downgraded", out["final_status"] == "needs_review")
    i2 = Incident("INC-y", ["test"], e)
    out2 = i2.conclude("recovered", "unsure", 0.1, [])
    check("low confidence cannot claim repair", out2["final_status"] == "needs_review")


@reports
def test_failed_monitoring_keeps_the_incident_open() -> None:
    """A repair that verifies but does not HOLD must not close the incident.

    The agent claims 'recovered'; the monitoring window rejects the claim.
    The incident has to return to investigating and the loop's terminal
    predicate has to stay False, so the agent keeps working rather than
    closing a station that is still broken.
    """
    print("\nRejected conclusion")
    from app.agent import tools as agent_tools
    from app.agent.incident import Incident

    e = Engine(); e.load_scenario("scaling", seed=7); settle(e, 30)
    incident = Incident("INC-monitor", ["test"], e)
    box = agent_tools.ToolBox(e, incident)

    original = agent_tools.score_live_samples
    agent_tools.score_live_samples = lambda *a, **k: {
        "passed": False,
        "checks": [{"name": "pressure_within_band", "passed": False}]}
    try:
        out = asyncio.run(box.t_conclude("recovered", "claiming a repair",
                                         0.95, [], ""))
    finally:
        agent_tools.score_live_samples = original

    check("a repair that does not hold is rejected",
          out.get("error") == "recovery_did_not_hold")
    check("the incident returns to investigating",
          incident.state == "investigating")
    check("no final status is recorded", "final_status" not in out)
    # Ask the agent loop itself, rather than restating its rule here.
    from app.agent.loop import ends_the_run
    check("the agent keeps working instead of closing",
          ends_the_run("conclude", out) is False)
    check("an accepted conclusion still ends the run",
          ends_the_run("conclude", {"final_status": "recovered"}) is True)


@reports
def test_healthy_station_is_left_alone() -> None:
    print("\nHealthy station")
    e = Engine(); e.load_scenario("healthy", seed=7)
    settle(e, 90)
    check("no incident is raised on a healthy station", e.incident_id is None)
    check("pressure holds the setpoint", abs(e.plant.truth.pressure_bar - 4.0) < 0.15,
          f"{e.plant.truth.pressure_bar:.3f} bar")


@reports
def test_model_failure_does_not_fake_recovery() -> None:
    print("\nModel failure behaviour")
    from app.agent.incident import Incident
    from app.agent.loop import AgentRunner

    async def go():
        e = Engine(); e.load_scenario("scaling", seed=7); settle(e, 35)
        i = Incident("INC-z", ["test"], e)
        r = AgentRunner(e, i, provider="anthropic")   # no key configured
        os.environ.pop("ANTHROPIC_API_KEY", None)
        settings.anthropic_api_key = ""
        return await r.run()

    out = asyncio.run(go())
    check("unreachable model yields needs_review", out["status"] == "needs_review")
    check("no configuration was changed", not out["executions"])
    check("the failure is recorded on the incident", bool(out.get("error")))


if __name__ == "__main__":
    settings.autonomy = "full"
    for fn in (test_current_loop_maths, test_ground_truth_is_not_leaked,
               test_validator_refusals, test_executor_requires_passing_test,
               test_failover_refused_without_reference, test_containment_isolation,
               test_invariants_on_conclude,
               test_failed_monitoring_keeps_the_incident_open,
               test_healthy_station_is_left_alone,
               test_model_failure_does_not_fake_recovery):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all unit tests passed")
