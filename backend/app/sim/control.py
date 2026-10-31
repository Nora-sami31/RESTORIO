"""Allowed actions, validator and executor.

Specification sections 05 (steps 5-7) and 08.  The separation enforced here is
the core safety claim of the project:

    the model proposes  ->  the validator tests  ->  the executor alone writes

The model never touches plant state.  A proposal that fails validation, or
that is bound to a configuration revision the station has already moved past,
cannot be applied.
"""
from __future__ import annotations

import time
from typing import Any

from ..config import settings
from ..events import bus
from .plant import ma_to_pressure
from .records import (
    QUALITY_BAD,
    QUALITY_GOOD,
    QUALITY_STALE,
    Execution,
    Proposal,
    TestReport,
    VerificationResult,
    new_id,
    now,
)

# ---------------------------------------------------------------------------
# Action catalogue. Anything not in here cannot be proposed, tested or applied.
# ---------------------------------------------------------------------------
ACTIONS: dict[str, dict[str, Any]] = {
    "apply_config_patch": {
        "risk": "high",
        "target": "PLC-101",
        "allowed_fields": {
            "pt101_range_min": (0.0, 50.0),
            "pt101_range_max": (0.1, 50.0),
        },
        "description": ("Change validated PLC-101 measurement scaling. The "
                        "process setpoint is deliberately NOT writable: moving "
                        "it to match a suspect reading redefines success "
                        "instead of recovering the plant."),
    },
    "switch_feedback_source": {
        "risk": "high",
        "target": "PLC-101",
        "allowed_fields": {"feedback_source": ("PT-101", "PT-102")},
        "description": "Move the controller onto the predefined trusted feedback path.",
    },
    "contain_client": {
        "risk": "high",
        "target": "telemetry-gateway",
        "allowed_fields": {"client_id": None, "activate_decoy": None},
        "description": "Revoke a client's operational write access and route it to the decoy.",
    },
    "restart_forwarding_service": {
        "risk": "medium",
        "target": "telemetry-gateway",
        "allowed_fields": {},
        "description": ("Restart the gateway's telemetry forwarding path, "
                        "clearing a retry backlog and resuming live updates."),
    },
    "restore_automatic_control": {
        "risk": "high",
        "target": "PLC-101",
        "allowed_fields": {},
        "description": ("Return PLC-101 to automatic control when it has been "
                        "left in manual, so the loop closes again."),
    },
    "set_safe_state": {
        "risk": "medium",
        "target": "PLC-101",
        "allowed_fields": {},
        "description": "Place the controller in its predefined protective state.",
    },
    "clear_safe_state": {
        "risk": "high",
        "target": "PLC-101",
        "allowed_fields": {},
        "description": "Return the controller to automatic after the cause is addressed.",
    },
    "request_human_review": {
        "risk": "low",
        "target": "incident",
        "allowed_fields": {"reason": None},
        "description": "Escalate to an engineer. Always available; never blocked.",
    },
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


# ---------------------------------------------------------------------------
# Shared state evaluation. Used for both candidate tests and final verification
# so that "it passed in test" and "it passed for real" mean the same thing.
# ---------------------------------------------------------------------------
def evaluate_state(plant, gateway, settle_s: float, measure_s: float,
                   contained_clients: list[str] | None = None,
                   reference_setpoint: float | None = None) -> dict[str, Any]:
    """Run the station forward and score it against the recovery checks.

    `reference_setpoint` pins the target the deviation is measured against, so
    a proposal cannot appear to improve the process by moving the setpoint.
    """
    dt = settings.dt
    target_sp = reference_setpoint if reference_setpoint is not None else plant.config.setpoint_bar
    for _ in range(int(settle_s / dt)):
        plant.step(dt)

    agree_gaps: list[float] = []
    band_devs: list[float] = []
    ref_qualities: list[str] = []
    tripped = False

    for _ in range(int(measure_s / dt)):
        plant.step(dt)
        ref_val, ref_q, _age = plant.pt102_value()
        ref_qualities.append(ref_q)
        if plant.truth.tripped:
            tripped = True
        if ref_val is None:
            continue
        if plant.config.feedback_source == "PT-102":
            # Cyber branch: the quarantined feed is not required to agree.
            band_devs.append(abs(ref_val - target_sp))
        else:
            fwd = gateway.forwarded_value()
            agree_gaps.append(abs(fwd - ref_val))
            band_devs.append(abs(ref_val - target_sp))

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str, measured: Any = None) -> None:
        checks.append({"name": name, "passed": bool(passed),
                       "detail": detail, "measured": measured})

    ref_usable = bool(ref_qualities) and all(
        q not in (QUALITY_BAD, QUALITY_STALE) for q in ref_qualities)
    check("reference_path_usable", ref_usable,
          f"PT-102 quality across the window: {sorted(set(ref_qualities)) or ['none']}.",
          sorted(set(ref_qualities)))

    if agree_gaps:
        worst = max(agree_gaps)
        check("measurement_agreement", worst <= settings.agreement_tol_bar,
              f"Worst primary/reference disagreement {worst:.3f} bar "
              f"(tolerance {settings.agreement_tol_bar} bar).", round(worst, 3))
    else:
        check("measurement_agreement", ref_usable,
              "Controller is on the trusted path; the quarantined feed is not "
              "required to agree (specification section 10).", None)

    if band_devs:
        worst_dev = max(band_devs)
        check("process_in_target_band", worst_dev <= settings.target_band_bar,
              f"Worst deviation from the {target_sp:.1f} bar target was "
              f"{worst_dev:.3f} bar (band +/-{settings.target_band_bar}).",
              round(worst_dev, 3))
    else:
        check("process_in_target_band", False,
              "No usable independent measurement; the target band cannot be "
              "confirmed.", None)

    check("no_protective_trip", not tripped,
          "Protective trip did not activate during the measurement window."
          if not tripped else "Protective trip activated during the window.", tripped)

    if contained_clients:
        proof = gateway.containment_proof(contained_clients[0])
        check("containment_holds", proof["isolation_holds"],
              f"Probe write from {contained_clients[0]} reached route "
              f"'{proof['probe_route']}'; operational state changed: "
              f"{proof['reached_operational_state']}.", proof)

    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "final_true_pressure": round(plant.truth.pressure_bar, 3),   # evaluation only
    }


class Validator:
    """Structural checks, prerequisites and an isolated candidate test."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def validate(self, proposal: Proposal) -> TestReport:
        checks: list[dict[str, Any]] = []

        def add(name: str, passed: bool, detail: str) -> None:
            checks.append({"name": name, "passed": bool(passed), "detail": detail})

        spec = ACTIONS.get(proposal.action)
        add("action_permitted", spec is not None,
            f"Action '{proposal.action}' is "
            + ("in the allowed catalogue." if spec else "NOT an allowed action."))
        if spec is None:
            return self._report(proposal, checks, "Rejected: unknown action.")

        # -- revision guard: the station must not have moved underneath us --
        current_rev = self.engine.plant.config_revision
        fresh = (proposal.bound_revision == current_rev
                 or "revision_guard" in settings.ablate)
        add("bound_to_current_revision", fresh,
            f"Proposal is bound to revision {proposal.bound_revision}; station is "
            f"at revision {current_rev}."
            + ("" if fresh else " Stale proposal must be rebuilt."))
        if not fresh:
            return self._report(proposal, checks,
                                "Rejected: configuration changed during investigation.")

        # -- field whitelist and bounds --------------------------------------
        allowed = spec["allowed_fields"]
        if "field_whitelist" in settings.ablate:
            allowed = None
        for key, val in (proposal.changes or {}).items():
            if allowed is None and "field_whitelist" in settings.ablate:
                add(f"field_allowed:{key}", True,
                    f"'{key}' = {val} accepted (whitelist ABLATED).")
                continue
            if allowed is None or key not in allowed:
                add(f"field_allowed:{key}", False,
                    f"Field '{key}' is not writable by '{proposal.action}'.")
                continue
            bounds = allowed[key]
            if isinstance(bounds, tuple) and bounds and isinstance(bounds[0], (int, float)):
                lo, hi = bounds
                ok = isinstance(val, (int, float)) and lo <= float(val) <= hi
                add(f"field_allowed:{key}", ok,
                    f"'{key}' = {val} "
                    + (f"is inside the permitted range [{lo}, {hi}]." if ok
                       else f"is OUTSIDE the permitted range [{lo}, {hi}]."))
            elif isinstance(bounds, tuple):
                ok = val in bounds
                add(f"field_allowed:{key}", ok,
                    f"'{key}' = {val} " + ("is permitted." if ok
                                           else f"must be one of {list(bounds)}."))
            else:
                add(f"field_allowed:{key}", True, f"'{key}' = {val} accepted.")

        if not all(c["passed"] for c in checks):
            return self._report(proposal, checks, "Rejected: field validation failed.")

        # -- prerequisites ----------------------------------------------------
        if proposal.action == "switch_feedback_source" and \
                proposal.changes.get("feedback_source") == "PT-102" and \
                "prerequisites" not in settings.ablate:
            m = self.engine.plant.measurement_pt102()
            ok = m.value is not None and m.quality == QUALITY_GOOD \
                and m.age_s <= settings.staleness_limit_s
            add("fallback_freshness", ok,
                f"PT-102 quality '{m.quality}', age {m.age_s:.1f} s, value {m.value}. "
                + ("Fallback is usable." if ok else
                   "Fallback is NOT usable; failover must not be claimed."))
            if not ok:
                return self._report(proposal, checks,
                                    "Rejected: fallback path failed its freshness check.")

        if proposal.action == "contain_client" and "evidence_gate" not in settings.ablate:
            # Containing a client is an accusation. "This client wrote
            # something once" is not enough to make one: an operator
            # acknowledging an alarm has also written something. Four things
            # must hold together before containment is evidence-supported.
            cid = proposal.changes.get("client_id")
            gw, plant = self.engine.gateway, self.engine.plant

            known = cid in gw.known_clients
            add("client_known", known,
                f"Client '{cid}' is "
                + ("a known gateway client." if known else "not known to the gateway."))

            # (1) every cited record must actually exist.
            valid = {ev.evidence_id for ev in self.engine.evidence}
            valid |= {ev.evidence_id for ev in gw.access_evidence()}
            unknown = [e for e in (proposal.evidence_ids or []) if e not in valid]
            cited_ok = bool(proposal.evidence_ids) and not unknown
            add("cited_evidence_exists", cited_ok,
                "All cited records resolve." if cited_ok else
                (f"Cited records do not exist: {unknown}. A containment "
                 "proposal must rest on real evidence."
                 if unknown else
                 "No evidence was cited at all."))

            # (2) the client must have written a field that actually reaches
            #     operational telemetry -- not merely written *something*.
            TELEMETRY_FIELDS = {"pt101_forwarded_bar", "pt101_forwarded_offset_bar"}
            writes = [ev for ev in gw.access_log
                      if ev.client_id == cid and ev.action == "write"
                      and ev.authorized and ev.field_name in TELEMETRY_FIELDS]
            add("client_wrote_operational_telemetry", bool(writes),
                (f"Trusted access log records {len(writes)} write(s) by this "
                 f"client to a forwarded telemetry field.") if writes else
                (f"The access log records no write by '{cid}' to any forwarded "
                 "telemetry field. Other activity by a client is not grounds "
                 "for containment."))

            # (3) that write must still be the thing shaping the forwarded value.
            st = gw.status()
            in_effect = (st["override_author"] == cid
                         or (st["offset_author"] == cid
                             and abs(st["forwarded_offset_bar"]) > 1e-9))
            add("write_still_in_effect", in_effect,
                "The value this client wrote is still shaping the forwarded "
                "telemetry." if in_effect else
                "Nothing this client wrote is currently affecting the forwarded "
                "value, so containing it now would not address the discrepancy.")

            # (4) there must be a discrepancy for the write to explain.
            local = plant.pt101_local_value()
            fwd = gw.forwarded_value()
            gap = abs(fwd - local)
            attributable = gap > settings.agreement_tol_bar
            add("discrepancy_attributable_to_write", attributable,
                f"The forwarded value differs from the local read by {gap:.3f} bar, "
                f"which the recorded write explains." if attributable else
                f"Forwarded and local agree to {gap:.3f} bar. There is no "
                "telemetry discrepancy for this client to be responsible for.")

            if not (known and cited_ok and writes and in_effect and attributable):
                return self._report(
                    proposal, checks,
                    "Rejected: containment is an accusation and this one is not "
                    "supported by the evidence.")

        if proposal.action == "restore_automatic_control":
            mode = self.engine.plant.config.controller_mode
            ok = mode == "manual"
            add("controller_is_in_manual", ok,
                f"PLC-101 is in '{mode}' mode. "
                + ("The loop is open and can be closed."
                   if ok else
                   "This action only applies to a controller left in manual."))
            if not ok:
                return self._report(proposal, checks,
                                    "Rejected: the controller is not in manual.")

        if proposal.action == "restart_forwarding_service":
            # Must be justified by what the metrics show, not by a hunch. This
            # is the observable equivalent of the scaling claim check.
            m = self.engine.read_metrics("gateway.comms_retries_per_min", 30.0)
            rate = self.engine.read_metrics("gateway.forward_rate_hz", 30.0)
            degraded = ((m.get("latest") or 0) > 10.0
                        or (rate.get("latest") is not None and rate["latest"] < 1.0))
            add("forwarding_fault_supported", degraded,
                f"Forwarding retries {m.get('latest')}/min and forward rate "
                f"{rate.get('latest')} Hz over the last 30 s. "
                + ("The metrics support a forwarding fault."
                   if degraded else
                   "The metrics show a healthy forwarding path; a restart is "
                   "not supported by the evidence."))
            if not degraded:
                return self._report(proposal, checks,
                                    "Rejected: the forwarding path is healthy.")

        if proposal.action == "clear_safe_state" and not self.engine.plant.truth.tripped \
                and self.engine.plant.config.controller_mode != "safe_state":
            add("safe_state_active", False, "Controller is not in a safe state.")
            return self._report(proposal, checks, "Rejected: nothing to clear.")

        # -- the response must be supported by the measurements ---------------
        if proposal.action == "apply_config_patch" and "pt101_range_max" in proposal.changes \
                and "claim_check" not in settings.ablate:
            raw = self.engine.plant.measurement_pt101_local().raw_signal_ma
            ref, ref_q, _age = self.engine.plant.pt102_value()
            implied = ma_to_pressure(raw, float(proposal.changes.get("pt101_range_min", 0.0)),
                                     float(proposal.changes["pt101_range_max"]))
            rate = self.engine.read_metrics("pt102.update_rate_hz", 30.0)
            nominal = 1.0 / settings.dt
            intermittent = (rate.get("mean") is not None
                            and rate["mean"] < nominal * 0.6)
            if ref is None or ref_q in (QUALITY_BAD, QUALITY_STALE):
                add("scaling_claim_supported", False,
                    f"No trustworthy independent reference (quality '{ref_q}') against "
                    "which to justify a scaling change.")
            elif intermittent:
                # Quality is a snapshot. An instrument that barely updates can
                # look clean at the instant you sample it and still be useless
                # as an independent measurement.
                add("scaling_claim_supported", False,
                    f"The reference is updating at {rate['mean']:.2f} Hz against a "
                    f"nominal {nominal:.1f} Hz. An intermittent instrument cannot "
                    f"justify a scaling change, however good a single sample looks.")
            else:
                ok = abs(implied - ref) <= settings.agreement_tol_bar * 2
                add("scaling_claim_supported", ok,
                    f"Decoding the live {raw:.2f} mA signal with the proposed "
                    f"0-{proposal.changes['pt101_range_max']:g} bar range gives "
                    f"{implied:.2f} bar against an independent reference of {ref:.2f} bar. "
                    + ("The proposed range reproduces the reference."
                       if ok else
                       "The proposed range does NOT reproduce the reference, so it is "
                       "not the supported scaling."))
            if not all(ch["passed"] for ch in checks):
                return self._report(proposal, checks,
                                    "Rejected: the proposed scaling is not supported by "
                                    "the independent measurement.")

        # -- isolated candidate test ------------------------------------------
        #
        # The bar here is "safe, and an improvement the evidence supports" --
        # NOT "single-handedly restores the plant". When two faults are present,
        # no one bounded action can fully recover the station, and demanding
        # that would deadlock the agent into doing nothing. Live verification is
        # what decides whether the incident is actually over.
        if proposal.action in ("apply_config_patch", "switch_feedback_source",
                               "contain_client", "clear_safe_state",
                               "restart_forwarding_service",
                               "restore_automatic_control") \
                and "candidate_test" not in settings.ablate:
            baseline = self._sandbox_apply(proposal, apply_changes=False)
            sandbox = self._sandbox_apply(proposal)

            def worst(result, name):
                for ch in result["checks"]:
                    if ch["name"] == name and isinstance(ch.get("measured"), (int, float)):
                        return float(ch["measured"])
                return None

            for name in ("no_protective_trip", "containment_holds"):
                for ch in sandbox["checks"]:
                    if ch["name"] == name:
                        checks.append({"name": f"candidate:{name}",
                                       "passed": ch["passed"], "detail": ch["detail"]})

            before_dev = worst(baseline, "process_in_target_band")
            after_dev = worst(sandbox, "process_in_target_band")
            if before_dev is not None and after_dev is not None:
                improved = after_dev <= before_dev + 0.02
                add("candidate:no_regression", improved,
                    f"Worst deviation from setpoint changes from {before_dev:.3f} bar "
                    f"to {after_dev:.3f} bar on the isolated copy. "
                    + ("This response does not make the process worse."
                       if improved else "This response makes the process WORSE."))
            fully = sandbox["passed"]
            checks.append({
                "name": "candidate:predicted_full_recovery", "passed": True,
                "detail": ("The isolated copy fully recovers with this response alone."
                           if fully else
                           "The isolated copy does NOT fully recover with this response "
                           "alone -- a further cause is likely also present. This is "
                           "informational and does not block application."),
                "informational": True, "predicted_full_recovery": fully})

        passed = all(c["passed"] for c in checks)
        summary = ("Candidate is safe, evidence-supported and non-regressive."
                   + ("" if not passed else "")
                   if passed else
                   "Candidate failed at least one check and must not be applied.")
        return self._report(proposal, checks, summary)

    def _sandbox_apply(self, proposal: Proposal,
                       apply_changes: bool = True) -> dict[str, Any]:
        """Apply the proposal to a throwaway copy and score the outcome."""
        plant = self.engine.plant.clone()
        gw = self.engine.gateway.clone()
        gw.bind_local_source(plant.pt101_local_value)
        plant.set_forwarded_hook(gw.forwarded_value)

        contained: list[str] = []
        baseline_sp = self.engine.plant.config.setpoint_bar
        if not apply_changes:
            return evaluate_state(plant, gw, settle_s=settings.settle_seconds * 1.6,
                                  measure_s=settings.settle_seconds,
                                  reference_setpoint=baseline_sp)
        if proposal.action == "apply_config_patch":
            for k, v in proposal.changes.items():
                setattr(plant.config, k, float(v))
            plant.reset_integral()
        elif proposal.action == "switch_feedback_source":
            plant.config.feedback_source = proposal.changes["feedback_source"]
            plant.reset_integral()
        elif proposal.action == "contain_client":
            cid = proposal.changes["client_id"]
            gw.contain_client(cid, activate_decoy=bool(
                proposal.changes.get("activate_decoy", True)))
            contained = [cid]
            plant.reset_integral()
        elif proposal.action == "restore_automatic_control":
            plant.config.controller_mode = "auto"
            plant.reset_integral()
        elif proposal.action == "restart_forwarding_service":
            gw.restart_forwarding()
            plant.reset_integral()
        elif proposal.action == "clear_safe_state":
            plant.clear_trip()

        return evaluate_state(plant, gw, settle_s=settings.settle_seconds * 1.6,
                              measure_s=settings.settle_seconds,
                              contained_clients=contained,
                              reference_setpoint=baseline_sp)

    def _report(self, proposal: Proposal, checks: list[dict[str, Any]],
                summary: str) -> TestReport:
        rep = TestReport(
            test_id=new_id("TST"), proposal_id=proposal.proposal_id,
            passed=all(c["passed"] for c in checks),
            bound_revision=proposal.bound_revision, checks=checks, summary=summary,
        )
        bus.emit("test_report", rep.to_dict())
        return rep


class Executor:
    """The only component permitted to change operational state."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.executions: list[Execution] = []

    def apply(self, proposal: Proposal, report: TestReport | None) -> Execution:
        prior = self.engine.plant.config_revision

        def reject(detail: str) -> Execution:
            ex = Execution(
                execution_id=new_id("EXE"), proposal_id=proposal.proposal_id,
                test_id=report.test_id if report else None, prior_revision=prior,
                new_revision=prior, applied_at=now(), outcome="rejected", detail=detail,
            )
            self.executions.append(ex)
            bus.emit("execution", ex.to_dict())
            return ex

        if proposal.action not in ACTIONS:
            return reject(f"Unknown action '{proposal.action}'.")
        if proposal.action == "request_human_review":
            ex = Execution(
                execution_id=new_id("EXE"), proposal_id=proposal.proposal_id,
                test_id=None, prior_revision=prior, new_revision=prior,
                applied_at=now(), outcome="applied",
                detail=f"Escalated to human review: {proposal.changes.get('reason', '')}",
                affected=["incident"],
            )
            self.executions.append(ex)
            bus.emit("execution", ex.to_dict())
            return ex

        if (report is None or not report.passed) and \
                "require_passing_test" not in settings.ablate:
            return reject("No passing test report; application refused.")
        if report.proposal_id != proposal.proposal_id:
            return reject("Test report does not belong to this proposal.")
        if report.bound_revision != prior and "revision_guard" not in settings.ablate:
            return reject(
                f"Configuration moved from revision {report.bound_revision} to "
                f"{prior} after testing; application refused.")

        affected: list[str] = []
        detail = ""
        try:
            if proposal.action == "apply_config_patch":
                before = self.engine.plant.config.snapshot()
                for k, v in proposal.changes.items():
                    setattr(self.engine.plant.config, k, float(v))
                self.engine.plant.reset_integral()
                after = self.engine.plant.config.snapshot()
                changed = {k: {"before": before[k], "after": after[k]}
                           for k in after if before[k] != after[k]}
                detail = f"Applied configuration patch: {changed}"
                affected = ["PLC-101", "PT-101"]
            elif proposal.action == "switch_feedback_source":
                before = self.engine.plant.config.feedback_source
                self.engine.plant.config.feedback_source = proposal.changes["feedback_source"]
                self.engine.plant.reset_integral()
                detail = (f"Controller feedback moved from {before} to "
                          f"{self.engine.plant.config.feedback_source}.")
                affected = ["PLC-101", "PT-102"]
            elif proposal.action == "contain_client":
                res = self.engine.gateway.contain_client(
                    proposal.changes["client_id"],
                    activate_decoy=bool(proposal.changes.get("activate_decoy", True)))
                self.engine.plant.reset_integral()
                detail = f"Containment applied: {res}"
                affected = ["telemetry-gateway", "decoy-endpoint"]
            elif proposal.action == "restore_automatic_control":
                before = self.engine.plant.config.controller_mode
                self.engine.plant.config.controller_mode = "auto"
                self.engine.plant.reset_integral()
                detail = f"Controller returned from '{before}' to automatic control."
                affected = ["PLC-101", "P-101"]
            elif proposal.action == "restart_forwarding_service":
                res = self.engine.gateway.restart_forwarding()
                self.engine.plant.reset_integral()
                detail = f"Telemetry forwarding path restarted: {res}"
                affected = ["telemetry-gateway", "PLC-101"]
            elif proposal.action == "set_safe_state":
                self.engine.plant.config.controller_mode = "safe_state"
                detail = "Controller placed in predefined protective state."
                affected = ["PLC-101", "P-101"]
            elif proposal.action == "clear_safe_state":
                self.engine.plant.clear_trip()
                self.engine.plant.config.controller_mode = "auto"
                detail = "Controller returned to automatic control."
                affected = ["PLC-101", "P-101"]
        except Exception as exc:                      # noqa: BLE001
            return reject(f"Executor error: {exc!r}")

        new_rev = self.engine.plant.bump_revision()
        ex = Execution(
            execution_id=new_id("EXE"), proposal_id=proposal.proposal_id,
            test_id=report.test_id, prior_revision=prior, new_revision=new_rev,
            applied_at=now(), outcome="applied", detail=detail, affected=affected,
        )
        self.executions.append(ex)
        self.engine.add_marker("applied", f"Applied: {proposal.action}")
        bus.emit("execution", ex.to_dict())
        return ex

    # -- final, independent verification ----------------------------------
    async def verify(self, incident_id: str) -> VerificationResult:
        """Wait for the live station to settle, then score real samples."""
        await await_sim_seconds(self.engine,
                                settings.settle_seconds * 1.6 + settings.settle_seconds)
        contained = sorted(self.engine.gateway.contained)
        res = score_live_samples(self.engine, settings.settle_seconds,
                                 contained_clients=contained or None)
        vr = VerificationResult(
            verification_id=new_id("VER"), incident_id=incident_id,
            passed=res["passed"], checks=res["checks"],
            summary=("Independent checks on the live station confirm recovery."
                     if res["passed"] else
                     "Independent checks on the live station do NOT confirm recovery."),
        )
        self.engine.add_marker("verified" if res["passed"] else "verify_failed",
                               vr.summary)
        bus.emit("verification", vr.to_dict())
        return vr


# ---------------------------------------------------------------------------
# Live verification.
#
# The candidate test scores a *clone*. Final verification scores the REAL
# station: it waits for the process to settle, then reads the samples the
# running plant actually produced. That is a materially stronger claim than
# projecting a copy forward, and it is what the operator sees on the trend.
# ---------------------------------------------------------------------------
async def await_sim_seconds(engine, seconds: float, timeout_s: float = 90.0) -> None:
    """Let `seconds` of simulated time pass on the live station."""
    import asyncio

    target = engine.plant.sim_time + seconds
    deadline = time.monotonic() + timeout_s
    while engine.plant.sim_time < target:
        if engine.running:
            await asyncio.sleep(0.01)
        else:
            engine.tick()                     # headless / unit-test path
        if time.monotonic() > deadline:
            return


def score_live_samples(engine, measure_s: float,
                       contained_clients: list[str] | None = None) -> dict[str, Any]:
    """Score the samples the live plant genuinely produced over the window."""
    cutoff = engine.plant.sim_time - measure_s
    window = [s for s in engine.samples if s.get("sim_t", 0.0) >= cutoff]

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str, measured: Any = None) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail,
                       "measured": measured})

    if not window:
        check("measurement_window_available", False,
              "No live samples were recorded for the verification window.")
        return {"passed": False, "checks": checks, "samples": 0}

    qualities = [s["pt102_quality"] for s in window]
    ref_usable = all(q not in (QUALITY_BAD, QUALITY_STALE) for q in qualities) \
        and all(s["pt102_bar"] is not None for s in window)
    check("reference_path_usable", ref_usable,
          f"PT-102 quality across {len(window)} live samples: "
          f"{sorted(set(qualities))}.", sorted(set(qualities)))

    gaps, devs = [], []
    tripped = any(s["tripped"] for s in window)
    on_trusted = window[-1]["feedback_source"] == "PT-102"
    for s in window:
        ref = s["pt102_bar"]
        if ref is None:
            continue
        devs.append(abs(ref - s["setpoint_bar"]))
        if not on_trusted:
            gaps.append(abs(s["pt101_forwarded_bar"] - ref))

    if gaps:
        worst = max(gaps)
        check("measurement_agreement", worst <= settings.agreement_tol_bar,
              f"Worst live primary/reference disagreement {worst:.3f} bar "
              f"(tolerance {settings.agreement_tol_bar} bar), over {len(gaps)} samples.",
              round(worst, 3))
    else:
        check("measurement_agreement", ref_usable,
              "Controller is on the trusted path; the quarantined feed is not "
              "required to agree (specification section 10).", None)

    if devs:
        worst_dev = max(devs)
        check("process_in_target_band", worst_dev <= settings.target_band_bar,
              f"Worst live deviation from {window[-1]['setpoint_bar']:.1f} bar "
              f"setpoint was {worst_dev:.3f} bar (band +/-{settings.target_band_bar}) "
              f"across {measure_s:.0f} s of settled operation.", round(worst_dev, 3))
    else:
        check("process_in_target_band", False,
              "No usable independent measurement; the target band cannot be "
              "confirmed.", None)

    check("no_protective_trip", not tripped,
          "Protective trip stayed clear throughout the live window." if not tripped
          else "Protective trip activated during the live window.", tripped)

    if contained_clients:
        proof = engine.gateway.containment_proof(contained_clients[0])
        check("containment_holds", proof["isolation_holds"],
              f"Live probe write from {contained_clients[0]} was routed to "
              f"'{proof['probe_route']}'; operational state changed: "
              f"{proof['reached_operational_state']}.", proof)

    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "samples": len(window)}
