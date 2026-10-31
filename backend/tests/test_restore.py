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
from app.agent import foresight
from app.store import forecast as risk

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


# ---------------------------------------------------------------------------
# Forecasting.  A forecast is the easiest place in this system to produce a
# confident number from nothing, so these tests exercise the refusals rather
# than the happy path.
# ---------------------------------------------------------------------------
NOW = 1_000_000.0


def _rows() -> list[dict]:
    """Three escalating pump incidents an hour apart, plus one-off classes."""
    return [
        {"scenario_key": "pump_degraded", "status": "needs_review",
         "created_at": NOW - 10800, "duration_s": 20.0,
         "diagnosis": "P-101 cannot reach setpoint; PT-101 agrees with PT-102"},
        {"scenario_key": "pump_degraded", "status": "needs_review",
         "created_at": NOW - 7200, "duration_s": 21.0,
         "diagnosis": "P-101 head loss consistent with impeller wear"},
        {"scenario_key": "pump_degraded", "status": "needs_review",
         "created_at": NOW - 3600, "duration_s": 19.0,
         "diagnosis": "P-101 output saturated, no software remedy"},
        {"scenario_key": "tamper", "status": "recovered_under_containment",
         "created_at": NOW - 1800, "duration_s": 110.0,
         "diagnosis": "GW-1 client altered forwarded PT-101 values"},
    ]


@reports
def test_forecast_refuses_to_project_from_one_occurrence() -> None:
    print("\nForecast: a single occurrence is not a pattern")
    r = risk.recurrence(_rows(), NOW)
    by = {c["scenario_key"]: c for c in r["classes"]}

    check("a class seen once has no interval",
          by["tamper"]["mean_interval_s"] is None)
    check("and therefore no due-in projection",
          by["tamper"]["due_in_s"] is None)
    check("it is labelled a single occurrence",
          by["tamper"]["basis"] == "single occurrence")
    check("and it cannot headline a forecast",
          by["tamper"]["headline_eligible"] is False)
    check("three occurrences can", by["pump_degraded"]["headline_eligible"] is True)
    check("the recorded span is reported, not assumed",
          abs(r["observed_span_h"] - 2.5) < 0.01, str(r["observed_span_h"]))


@reports
def test_forecast_drops_the_periodic_model_when_it_stops_fitting() -> None:
    print("\nForecast: 'overdue' is not claimed once the interval stops describing a class")
    rows = _rows()
    r = risk.recurrence(rows, NOW)
    pd = next(c for c in r["classes"] if c["scenario_key"] == "pump_degraded")
    check("while the gap matches the interval the model holds",
          pd["interval_model_holds"] is True, str(pd["elapsed_over_interval"]))

    # Same history, read a day later: the class has not recurred on anything
    # like its interval, so a periodic projection is no longer meaningful.
    late = risk.recurrence(rows, NOW + 86400)
    pdl = next(c for c in late["classes"] if c["scenario_key"] == "pump_degraded")
    check("a gap many times the interval drops the model",
          pdl["interval_model_holds"] is False, str(pdl["elapsed_over_interval"]))
    check("the ratio is reported so the operator can judge it",
          pdl["elapsed_over_interval"] > risk.MODEL_HOLDS_RATIO)
    check("the occurrence counts are untouched by the timing question",
          pdl["n"] == 3 and pdl["escalated"] == 3)


@reports
def test_forecast_claim_check_rejects_unsupported_claims() -> None:
    print("\nForecast: the claim check tests the conclusion against the record")
    hist = risk.recurrence(_rows(), NOW)

    def verdict(args):
        cs = foresight.check_claim(args, hist)
        return {c["name"]: c["passed"] for c in cs}

    good = {"claim": "x", "asset": "P-101", "mechanism": "wear",
            "horizon_h": 1.0, "confidence": 0.6, "basis": "3 occurrences",
            "scenario_keys": ["pump_degraded"]}
    check("a supported claim passes every check", all(verdict(good).values()))

    v = verdict(good | {"scenario_keys": ["not_a_real_class"]})
    check("a class absent from the record is rejected",
          v["classes_exist_in_record"] is False)

    v = verdict(good | {"scenario_keys": ["tamper"]})
    check("a class with too few occurrences is rejected",
          v["basis_thick_enough"] is False)

    v = verdict(good | {"horizon_h": 72.0})
    check("a horizon beyond the recorded history is rejected",
          v["horizon_within_record"] is False)

    v = verdict(good | {"confidence": 0.0})
    check("a zero confidence is rejected", v["confidence_in_range"] is False)

    v = verdict(good | {"asset": "PT-999"})
    check("an asset tag absent from the recorded diagnoses is rejected",
          v["asset_in_record"] is False)
    check("an asset the record does name is accepted",
          verdict(good | {"asset": "P-101"})["asset_in_record"] is True)

    empty = risk.recurrence([], NOW)
    check("with no history read, nothing can be claimed",
          foresight.check_claim(good, empty)[0]["passed"] is False)


@reports
def test_forecast_agent_cannot_change_the_plant() -> None:
    print("\nForecast: the forecasting agent has no write tools")
    names = {t["name"] for t in foresight.TOOL_SCHEMAS}
    mutating = {"test_response", "apply_validated_response", "verify_recovery",
                "conclude"}
    check("its tool surface is read-only plus its own conclusion",
          not (names & mutating), str(sorted(names & mutating)))
    check("it cannot reach the executor", "apply_validated_response" not in names)
    check("it has a history tool at all", "read_incident_history" in names)


@reports
def test_offline_forecast_passes_its_own_claim_check() -> None:
    print("\nForecast: the no-key baseline holds itself to the same checks")
    e = Engine()
    settle(e, 5.0)
    out = foresight.offline_forecast(e, _rows(), NOW)
    check("the rules baseline produces a forecast", out["available"] is True,
          str(out.get("reason")))
    check("and does not claim to be agentic", out["agentic"] is False)
    check("every check it is graded on passed",
          all(c["passed"] for c in out["checks"]))
    check("it names the class it relied on",
          out["scenario_keys"] == ["pump_degraded"])
    check("the basis quotes the counts",
          "3 recorded occurrences" in out["basis"], out["basis"])
    check("the asset comes from the record, not a constant",
          out["asset"] == "P-101", out["asset"])

    thin = foresight.offline_forecast(e, _rows()[:1], NOW)
    check("with one occurrence it declines to forecast",
          thin["available"] is False, str(thin.get("claim")))


@reports
def test_forecast_closes_the_review_loopholes() -> None:
    """Regression tests for five defects found by review of the forecast.

    Each one passed before the fix, so each is a real guard rather than a
    restatement of the implementation.
    """
    print("\nForecast: review findings stay closed")
    hist = risk.recurrence(_rows(), NOW)
    good = {"claim": "P-101 is the likely next escalation.", "asset": "P-101",
            "mechanism": "wear", "horizon_h": 1.0, "confidence": 0.6,
            "basis": "3 occurrences", "scenario_keys": ["pump_degraded"]}

    def verdict(args, h=hist):
        return {c["name"]: c["passed"] for c in foresight.check_claim(args, h)}

    # 3a. A one-occurrence class may not ride along on a well-evidenced one.
    v = verdict(good | {"scenario_keys": ["pump_degraded", "tamper"]})
    check("a thin class cited alongside a thick one is rejected",
          v["basis_thick_enough"] is False)

    # 3b. An asset that cannot be checked is not thereby acceptable.
    blank = risk.recurrence(
        [dict(r, diagnosis="no asset tags recorded") for r in _rows()], NOW)
    v = verdict(good | {"asset": "PT-999"}, blank)
    check("an asset claim with nothing to check it against is rejected",
          v["asset_in_record"] is False)

    # 2. A statement about WHEN needs a class recurring on an interval.
    scattered = risk.recurrence(
        [dict(r, created_at=t) for r, t in
         zip(_rows()[:3], [NOW - 9060, NOW - 9000, NOW])], NOW)
    sc = scattered["classes"][0]
    check("widely scattered gaps are not called periodic",
          sc["interval_model_holds"] is False, str(sc["interval_cv"]))
    v = verdict(good | {"claim": "The pump is overdue."}, scattered)
    check("an unsupported timing claim is rejected",
          v["timing_claim_supported"] is False)
    check("the same forecast without a timing claim is allowed",
          verdict(good, scattered)["timing_claim_supported"] is True)
    check("timing is allowed when a cited class really is periodic",
          verdict(good | {"claim": "Expected within the hour."})
          ["timing_claim_supported"] is True)

    # 4. One gap is not a period.
    one = risk.recurrence(_rows()[:2], NOW)
    check("a single interval cannot establish periodicity",
          one["classes"][0]["interval_model_holds"] is False)

    # 1. The record says what it is.
    check("the history reports its provenance",
          hist["provenance"] == "demonstration" and
          "triggered" in hist["provenance_note"])


@reports
def test_forecast_reports_a_pump_already_at_its_ceiling() -> None:
    print("\nForecast: saturation is reported, not mistaken for a flat signal")

    class Pinned:
        class plant:
            sim_time = 500.0

        class metrics:
            @staticmethod
            def read(name, window_s, now_t):
                if name == "pump.output_pct":
                    return {"points": [(t, 100.0) for t in range(440, 500, 2)],
                            "latest": 100.0}
                return {"latest": 2.5, "trend": 0.4}

    out = risk.project_live(Pinned(), 120.0)
    check("a pump pinned at its ceiling is reported as saturated",
          out.get("saturated") is True, str(out.get("reason")))
    check("and is not dismissed as 'not climbing'",
          "not climbing" not in str(out.get("reason") or ""))
    check("the detail explains the consequence",
          "no authority left" in (out.get("detail") or ""))


if __name__ == "__main__":
    settings.autonomy = "full"
    for fn in (test_current_loop_maths, test_ground_truth_is_not_leaked,
               test_validator_refusals, test_executor_requires_passing_test,
               test_failover_refused_without_reference, test_containment_isolation,
               test_invariants_on_conclude,
               test_failed_monitoring_keeps_the_incident_open,
               test_healthy_station_is_left_alone,
               test_model_failure_does_not_fake_recovery,
               test_forecast_refuses_to_project_from_one_occurrence,
               test_forecast_drops_the_periodic_model_when_it_stops_fitting,
               test_forecast_claim_check_rejects_unsupported_claims,
               test_forecast_agent_cannot_change_the_plant,
               test_offline_forecast_passes_its_own_claim_check,
               test_forecast_closes_the_review_loopholes,
               test_forecast_reports_a_pump_already_at_its_ceiling):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all unit tests passed")
