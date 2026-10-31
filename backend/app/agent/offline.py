"""Deterministic reasoner.

    FROZEN BASELINE. Do not extend this file to cover new fault classes.

This procedure was written with full knowledge of the authored scenarios: we
knew which discriminator separated each one before we wrote the rule. That
makes it an *upper bound* on what hand-written rules can do here, not a
baseline a team would realistically have deployed, and a tie against it means
considerably less than it looks.

It is frozen at the authored fault set so that generated incidents are a fair
test of it. Every time the fault space grew we previously extended these rules
to keep pace, which quietly flattered them. That stops here.

It also remains the system's degraded mode: if no model provider is reachable,
this is what keeps the station investigated and escalated rather than ignored.


Two jobs:

  1. The demonstration must run with no API key, no network and no budget.
     This drives the same tools through the same loop as a model would.
  2. Specification section 10 asks for a rules-based baseline to compare the
     model against.  This IS that baseline -- a competent, explicit decision
     procedure written by hand.

It is deliberately not clever.  It follows a fixed investigation order, then
applies one discriminator:

    decode the raw mA with the INSTALLED device range.
      if that matches the independent reference -> the configured range is wrong
    compare the forwarded value with the local read.
      if they differ and the gateway records a client write -> telemetry tampering

Both can be true at once.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import settings
from .brain import Brain, BrainReply, ToolCall

TOL = 0.25


class OfflineBrain(Brain):
    name = "offline"
    is_model = False

    def __init__(self) -> None:
        self.done: list[str] = []
        self.facts: dict[str, Any] = {}
        self.last: dict[str, Any] = {}
        self.turn = 0
        self.fixed: set[str] = set()
        self.failed_tests = 0
        self.verify_failures = 0
        self.pending_proposal: str | None = None
        self.concluded = False

    # -- message plumbing (state is kept here, not in the transcript) -----
    def assistant_turn(self, reply: BrainReply) -> dict[str, Any]:
        return {"role": "assistant", "content": reply.text or ""}

    def tool_results_turn(self, results: list[tuple[str, Any]]) -> dict[str, Any]:
        for _tid, out in results:
            if isinstance(out, dict):
                self._absorb(out)
        return {"role": "user", "content": json.dumps(
            [out for _t, out in results], default=str)[:4000]}

    # -- fact extraction ---------------------------------------------------
    def _absorb(self, out: dict[str, Any]) -> None:
        self.last = out
        f = self.facts

        if "now" in out and "controller" in out:                 # read_telemetry
            n = out["now"]
            f["fwd"] = n["pt101_forwarded"]["value"]
            f["local"] = n["pt101_local"]["value"]
            f["raw_ma"] = n["pt101_local"]["raw_signal_ma"]
            f["ref"] = n["pt102_reference"]["value"]
            f["ref_quality"] = n["pt102_reference"]["quality"]
            f["ref_age"] = n["pt102_reference"]["age_s"]
            f["feedback_source"] = out["controller"]["feedback_source"]
            f["controller_mode"] = out["controller"].get("mode")
            f["pump_pct"] = out["controller"].get("pump_output_pct")
            f["setpoint"] = out["controller"]["setpoint_bar"]
            f["pump"] = out["controller"]["pump_output_pct"]

        if out.get("asset_id") == "PT-101" and "installed_device" in out:
            f["installed_max"] = out["installed_device"]["range_max_bar"]
            f["installed_min"] = out["installed_device"]["range_min_bar"]
            f["configured_max"] = out["configured_scaling"]["range_max_bar"]
            f["configured_min"] = out["configured_scaling"]["range_min_bar"]

        if out.get("asset_id") == "telemetry-gateway":
            f["gw_modified"] = out.get("forwarded_field_modified", False)
            f["gw_author"] = out.get("forwarded_field_author")
            f["gw_contained"] = out.get("contained_clients", [])

        if "metrics" in out and isinstance(out.get("metrics"), list):
            for m in out["metrics"]:
                if m.get("metric") == "gateway.comms_retries_per_min":
                    f["gw_retries"] = m.get("latest")
                    f["gw_retries_trend"] = m.get("trend")
                if m.get("metric") == "gateway.forward_rate_hz":
                    f["gw_forward_rate"] = m.get("latest")
                if m.get("metric") == "pt102.update_rate_hz":
                    f["ref_update_rate"] = m.get("mean")

        if "implied_pressure_bar" in out:                        # check_scaling
            key = f"implied_{out['range_max']}"
            f[key] = out["implied_pressure_bar"]

        if "results" in out and isinstance(out["results"], list):  # search_evidence
            ids = f.setdefault("evidence_ids", [])
            for r in out["results"]:
                if r.get("evidence_id") and r["evidence_id"] not in ids:
                    ids.append(r["evidence_id"])
                p = r.get("payload") or {}
                if p.get("action") == "write" and p.get("authorized") and p.get("client_id"):
                    f["write_client"] = p["client_id"]
                    f["write_evidence"] = r["evidence_id"]

        if "proposal_id" in out and "passed" in out:             # test_response
            if out["passed"]:
                self.pending_proposal = out["proposal_id"]
            else:
                self.failed_tests += 1
                self.pending_proposal = None

        if "outcome" in out and out.get("outcome") in ("applied", "rejected"):
            if out["outcome"] == "applied":
                self.fixed.add(self.facts.get("_applying", "unknown"))
            self.pending_proposal = None

        if "passed" in out and "summary" in out and "checks" in out \
                and "proposal_id" not in out:                     # verify_recovery
            f["verified"] = out["passed"]
            if not out["passed"]:
                self.verify_failures += 1
                # Force a fresh look at the station before trying anything else.
                # read_metrics MUST be refreshed here. Without it the stalled
                # -forwarding fact stays latched after the restart, and that
                # permanently suppresses the tampering check -- so a second,
                # different fault behind a forwarding stall is never seen.
                # (State-management fix, not new fault coverage: the frozen
                # baseline gets to be correct about what it already knows.)
                for step in ("read_telemetry", "get_asset:PT-101",
                             "get_asset:telemetry-gateway", "check_installed",
                             "search:access", "read_metrics"):
                    if step in self.done:
                        self.done.remove(step)

    # -- diagnosis ---------------------------------------------------------
    def _scaling_wrong(self) -> bool:
        f = self.facts
        inst, conf = f.get("installed_max"), f.get("configured_max")
        if inst is None or conf is None or abs(inst - conf) < 1e-6:
            return False
        implied = f.get(f"implied_{inst}")
        ref = f.get("ref")
        if implied is None or ref is None:
            return False
        # The installed range explains the reference; the configured one does not.
        return abs(implied - ref) <= TOL and abs((f.get("local") or 0) - ref) > TOL

    def _forwarding_stalled(self) -> bool:
        """A held value, not an altered one. Metrics separate the two."""
        f = self.facts
        retries = f.get("gw_retries")
        rate = f.get("gw_forward_rate")
        return bool((retries is not None and retries > 10.0)
                    or (rate is not None and rate < 1.0))

    def _tampering(self) -> bool:
        f = self.facts
        fwd, local = f.get("fwd"), f.get("local")
        if fwd is None or local is None:
            return False
        if self._forwarding_stalled():
            return False          # the gap is staleness, not a write
        return abs(fwd - local) > TOL and bool(
            f.get("gw_modified") or f.get("write_client"))

    def _reference_usable(self) -> bool:
        """Good enough to compare against and to verify a repair with.

        Quality alone is a snapshot. An instrument that updates intermittently
        can show a clean flag at the instant you look and still be useless as
        an independent measurement, so the update rate is checked too.
        """
        f = self.facts
        rate = f.get("ref_update_rate")
        nominal = 1.0 / settings.dt
        if rate is not None and rate < nominal * 0.6:
            return False
        return (f.get("ref") is not None
                and f.get("ref_quality") not in ("bad", "stale")
                and (f.get("ref_age") or 0) <= settings.staleness_limit_s)

    def _reference_controllable(self) -> bool:
        """Certified to hand the controller to -- a stricter bar."""
        f = self.facts
        return self._reference_usable() and f.get("ref_quality") == "good"

    # Back-compat alias used by the decision points below.
    def _reference_trustworthy(self) -> bool:
        return self._reference_usable()

    # -- the fixed investigation order -------------------------------------
    def converse(self, system, messages, tools) -> BrainReply:
        self.turn += 1
        f = self.facts

        def call(name: str, args: dict[str, Any], note: str,
                 mark: str | None = None) -> BrainReply:
            self.done.append(mark or name)
            return BrainReply(text=note, tool_calls=[
                ToolCall(id=f"off_{self.turn}", name=name, arguments=args)])

        if "read_telemetry" not in self.done:
            return call("read_telemetry", {"window_s": 30},
                        "Reading the three measurement paths before forming any view.")

        if "get_asset:PT-101" not in self.done:
            return call("get_asset", {"asset_id": "PT-101"},
                        "Comparing the installed transmitter against the configured "
                        "scaling.", mark="get_asset:PT-101")

        if "check_installed" not in self.done and f.get("raw_ma") is not None \
                and f.get("installed_max") is not None:
            return call("check_scaling",
                        {"raw_signal_ma": f["raw_ma"], "range_min": f.get("installed_min", 0.0),
                         "range_max": f["installed_max"]},
                        f"Decoding {f['raw_ma']:.2f} mA with the INSTALLED "
                        f"0-{f['installed_max']:g} bar range to see which mapping "
                        "explains the reference reading.", mark="check_installed")

        if "get_asset:telemetry-gateway" not in self.done:
            return call("get_asset", {"asset_id": "telemetry-gateway"},
                        "Checking whether the forwarded telemetry field was modified.",
                        mark="get_asset:telemetry-gateway")

        if "read_metrics" not in self.done:
            return call("read_metrics", {"window_s": 120},
                        "Checking the gateway and controller metrics for the "
                        "state of the forwarding path.")

        if "search:records" not in self.done:
            return call("search_evidence",
                        {"query": "transmitter replacement range scaling backup restore"},
                        "Looking for maintenance and configuration history.",
                        mark="search:records")

        if "search:access" not in self.done:
            return call("search_evidence", {"query": "write client", "kind": "access_event"},
                        "Reading the trusted access log for operational writes.",
                        mark="search:access")

        if "compare_configs" not in self.done:
            return call("compare_configs", {"revision_a": 5, "revision_b": 0},
                        "Diffing the post-replacement revision against the live "
                        "configuration.")

        if "record_hypotheses" not in self.done:
            scaling, tamper = self._scaling_wrong(), self._tampering()
            ref_ok = self._reference_trustworthy()
            hyps = [
                {"name": "PT-101 scaling mismatch after backup restore",
                 "stance": "supported" if scaling else "contradicted",
                 "rationale": (
                     f"Raw {f.get('raw_ma', 0):.2f} mA decoded with the installed "
                     f"0-{f.get('installed_max')}-bar range gives "
                     f"{f.get('implied_' + str(f.get('installed_max'))):.2f} bar, which "
                     f"matches the reference {f.get('ref')}. The configured "
                     f"0-{f.get('configured_max')}-bar range gives {f.get('local')}."
                     if scaling else
                     "The configured range matches the installed device, or the "
                     "installed-range decode does not explain the reference."),
                 "evidence_ids": [e for e in f.get("evidence_ids", [])
                                  if e.startswith(("WO-", "DOC-", "CFG-"))][:4]},
                {"name": "Forwarded telemetry manipulated at the gateway",
                 "stance": "supported" if tamper else "contradicted",
                 "rationale": (
                     f"Forwarded {f.get('fwd')} differs from the local trusted read "
                     f"{f.get('local')} and the gateway records a write by "
                     f"{f.get('write_client') or f.get('gw_author')}."
                     if tamper else
                     "The forwarded value matches the local read; no unexplained "
                     "gateway write is present."),
                 "evidence_ids": [f["write_evidence"]] if f.get("write_evidence") else []},
                {"name": "Reference path unusable (stale or offline)",
                 "stance": "contradicted" if ref_ok else "supported",
                 "rationale": f"PT-102 quality '{f.get('ref_quality')}', age "
                              f"{f.get('ref_age')} s.",
                 "evidence_ids": []},
                {"name": "Physical process disturbance or pump fault",
                 "stance": "contradicted",
                 "rationale": "Pump and inspection records show no fault, and a real "
                              "disturbance would move both measurement paths together.",
                 "evidence_ids": ["NOTE-8839"]},
            ]
            return call("record_hypotheses", {"hypotheses": hyps},
                        "Recording the explanations I compared, including the ones "
                        "the evidence rules out.")

        # ---- response selection -------------------------------------------
        scaling, tamper = self._scaling_wrong(), self._tampering()
        ref_ok = self._reference_trustworthy()

        if self.pending_proposal:
            pid = self.pending_proposal
            return call("apply_validated_response", {"proposal_id": pid},
                        "Candidate passed every check; applying it.",
                        mark=f"apply:{pid}")

        # No trustworthy independent path -> protect and escalate, never claim
        # a failover that cannot be verified.
        if not ref_ok:
            if "safe_state" not in self.done and not f.get("safe_done"):
                f["safe_done"] = True
                f["_applying"] = "safe_state"
                return call("test_response", {
                    "action": "set_safe_state", "changes": {},
                    "evidence_ids": f.get("evidence_ids", [])[:3],
                    "rationale": "The independent reference is unusable, so no "
                                 "correction can be confirmed. Place the station in "
                                 "its predefined protective state and escalate."},
                    "No trustworthy independent measurement -- protecting the process "
                    "instead of guessing.", mark="safe_state")
            return self._conclude(
                "safe_state_unresolved" if f.get("safe_done") else "needs_review",
                "Primary and reference measurements disagree, but the independent "
                "reference path is unusable (quality "
                f"'{f.get('ref_quality')}', age {f.get('ref_age')} s). No correction "
                "can be independently confirmed and no validated fallback exists.",
                0.45,
                "Restore or replace PT-102, then re-run the investigation. Do not "
                "accept a scaling change without an independent reference.")

        if self.failed_tests >= 2 or self.turn > settings.max_iterations - 4:
            return self._conclude(
                "needs_review",
                "The evidence did not support a bounded response that passed "
                "validation. Escalating rather than applying an unsupported change.",
                0.4,
                "Engineer review required. Review the candidate test failures in the "
                "incident record.")

        # An open loop is a control fault, not a measurement one, and it has
        # an obvious fix. (Worn plant and a blocked line have no software fix,
        # so no rule is written for them -- escalation is already correct.)
        if f.get("controller_mode") == "manual" and "fix:manual" not in self.done:
            self.done.append("fix:manual")
            f["_applying"] = "manual"
            return call("test_response", {
                "action": "restore_automatic_control", "changes": {},
                "evidence_ids": ["NOTE-9321"],
                "rationale": (
                    "PLC-101 is in manual with its output held, so the loop is "
                    "open and the feedback is ignored. The measurements agree "
                    "and are correct. Close the loop.")},
                "Controller is in manual -- the loop is open.", mark="test:manual")

        if self._forwarding_stalled() and "fix:forwarding" not in self.done:
            self.done.append("fix:forwarding")
            f["_applying"] = "forwarding"
            return call("test_response", {
                "action": "restart_forwarding_service", "changes": {},
                "evidence_ids": ["NOTE-9201"],
                "rationale": (
                    f"Forwarding retries are {f.get('gw_retries')}/min and the "
                    f"forward rate is {f.get('gw_forward_rate')} Hz, so the "
                    f"controller is holding an old value rather than a wrong "
                    f"one. No client write appears in the access log.")},
                "Metrics show a stalled forwarding path, not an altered value.",
                mark="test:forwarding")

        if scaling and "fix:scaling" not in self.done:
            self.done.append("fix:scaling")
            f["_applying"] = "scaling"
            return call("test_response", {
                "action": "apply_config_patch",
                "changes": {"pt101_range_max": f["installed_max"],
                            "pt101_range_min": f.get("installed_min", 0.0)},
                "evidence_ids": [e for e in f.get("evidence_ids", [])
                                 if e.startswith(("WO-", "DOC-", "CFG-"))][:4],
                "rationale": (
                    f"The installed transmitter is a 0-{f['installed_max']:g} bar device, "
                    f"but PLC-101 decodes it with a 0-{f['configured_max']:g} bar range. "
                    f"Decoding the raw signal with the installed range reproduces the "
                    f"independent reference. Restore the supported scaling.")},
                "Testing the supported scaling correction on an isolated copy.",
                mark="test:scaling")

        if tamper and "fix:tamper" not in self.done:
            self.done.append("fix:tamper")
            f["_applying"] = "tamper"
            client = f.get("write_client") or f.get("gw_author") or "test-client-07"
            return call("test_response", {
                "action": "contain_client",
                "changes": {"client_id": client, "activate_decoy": True},
                "evidence_ids": ([f["write_evidence"]] if f.get("write_evidence") else [])
                                + [e for e in ("NOTE-8846",)
                                   if e in f.get("evidence_ids", [])],
                "rationale": (
                    f"The locally read value agrees with the independent reference, "
                    f"but the forwarded value does not, and the trusted access log "
                    f"records an operational write by {client}. Contain that client "
                    f"and route it to the decoy.")},
                "Local read agrees with the reference but the forwarded value does "
                "not -- testing containment of the writing client.",
                mark="test:tamper")

        # Containment stops further writes but the latched value persists, so
        # control must be moved onto the predefined trusted path.
        if tamper and "fix:tamper" in self.done and "fix:feedback" not in self.done \
                and self._reference_controllable() and f.get("feedback_source") == "PT-101":
            self.done.append("fix:feedback")
            f["_applying"] = "feedback"
            return call("test_response", {
                "action": "switch_feedback_source",
                "changes": {"feedback_source": "PT-102"},
                "evidence_ids": ([f["write_evidence"]] if f.get("write_evidence") else []),
                "rationale": (
                    "The client has been contained, but the value it wrote is still "
                    "latched in the forwarded field. PT-102 is fresh and good quality, "
                    "so move the controller onto the predefined trusted feedback path "
                    "to recover control.")},
                "Containment does not undo the latched value -- moving control to the "
                "verified reference path.", mark="test:feedback")

        if f.get("verified") is None and (self.fixed or "apply:" in " ".join(self.done)):
            return call("verify_recovery", {},
                        "Running the independent recovery checks.")

        if f.get("verified") is False:
            if self.verify_failures <= 2 and (scaling or tamper):
                return call("verify_recovery", {},
                            "Still not verified -- re-checking after the second repair.")
            return self._conclude(
                "needs_review",
                "A response was applied but the independent checks did not confirm "
                "recovery. A further cause is likely still present.",
                0.5,
                "Engineer review required: verification failed after remediation.")

        if f.get("verified") is True:
            contained = bool(f.get("gw_contained")) or "tamper" in self.fixed
            causes = []
            # self.fixed holds only responses the executor actually applied;
            # self.done also holds ones the validator refused.
            applied = self.fixed
            if "manual" in applied:
                causes.append(
                    "PLC-101 had been left in manual after instrument work, so "
                    "the loop was open and the controller ignored the feedback; "
                    "automatic control was restored")
            if "forwarding" in applied:
                causes.append(
                    "the gateway forwarding path had stalled and was holding a "
                    "stale value, so the controller never saw the disturbance; "
                    "the forwarding service was restarted")
            if "scaling" in applied:
                causes.append(
                    f"PT-101 was decoded with a superseded 0-{f.get('configured_max'):g} bar "
                    f"range although a 0-{f.get('installed_max'):g} bar transmitter is "
                    f"installed; the scaling was restored to the supported value")
            if "tamper" in applied:
                causes.append(
                    "the forwarded telemetry field had been modified by an "
                    "unauthorised client, which was contained and routed to the decoy")
            if "feedback" in applied:
                causes.append(
                    "control was moved onto the verified PT-102 reference path "
                    "because the latched forwarded value could not be trusted")
            return self._conclude(
                "recovered_under_containment" if contained else "recovered",
                ("Confirmed: " + "; and ".join(causes) + ".") if causes
                else "Station verified within tolerance.",
                0.88,
                ("Refresh the station backup image so the corrected scaling "
                 "survives the next restore."
                 if "scaling" in applied else
                 "Raise the stalled forwarding session with networks against "
                 "change notice NC-3340; the switch firmware fix is still "
                 "outstanding."
                 if "forwarding" in applied else
                 "Review the gateway write authorisation list before releasing "
                 "containment."))

        if "verify_recovery" not in self.done:
            return call("verify_recovery", {},
                        "Running the independent recovery checks.")

        return self._conclude(
            "needs_review",
            "Investigation completed without a validated, confirmable response.",
            0.4, "Engineer review required.")

    def _conclude(self, status: str, diagnosis: str, confidence: float,
                  note: str) -> BrainReply:
        self.concluded = True
        f = self.facts
        return BrainReply(text=f"Concluding: {status}.", tool_calls=[ToolCall(
            id=f"off_{self.turn}", name="conclude", arguments={
                "status": status, "diagnosis": diagnosis, "confidence": confidence,
                "evidence_ids": f.get("evidence_ids", [])[:6],
                "operator_note": note})])
