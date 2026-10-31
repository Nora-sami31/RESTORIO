"""Incident record and lifecycle.

Specification section 05.  Two invariants are enforced here rather than
trusted to the model:

  * a narrative cannot mark an incident recovered -- only a passing
    independent verification can;
  * a low-confidence conclusion cannot claim a repaired state, it is
    downgraded to needs_review.
"""
from __future__ import annotations

import time
from typing import Any

from ..config import settings
from ..events import bus
from ..sim.control import RISK_ORDER
from ..sim.records import (
    TERMINAL_STATES,
    Execution,
    Proposal,
    TestReport,
    VerificationResult,
    now,
)


class Incident:
    def __init__(self, incident_id: str, reasons: list[str], engine) -> None:
        self.incident_id = incident_id
        self.reasons = reasons
        self.engine = engine
        self.scenario_key = engine.scenario_key        # recorded for evaluation only
        self.created_at = now()
        self.state = "detected"
        self.timeline: list[dict[str, Any]] = []

        self.hypotheses: list[dict[str, Any]] = []
        self.proposals: list[Proposal] = []
        self.tests: list[TestReport] = []
        self.executions: list[Execution] = []
        self.verifications: list[VerificationResult] = []
        self.monitoring: list[dict[str, Any]] = []

        self.applies = 0
        self.approval_granted = False
        self.pending_approval: Proposal | None = None

        self.status: str | None = None
        self.diagnosis = ""
        self.confidence = 0.0
        self.operator_note = ""
        self.cited_evidence: list[str] = []
        self.closed_at: float | None = None
        self.agent_narrative: list[str] = []
        self.error: str | None = None
        self.usage = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}

        self.log("detected", "Incident detected", {"reasons": reasons})

    # ------------------------------------------------------------------
    def log(self, kind: str, label: str, data: dict[str, Any] | None = None) -> None:
        entry = {"kind": kind, "label": label, "ts": now(),
                 "elapsed_s": round(now() - self.created_at, 2), "data": data or {}}
        self.timeline.append(entry)
        bus.emit("incident_timeline", {"incident_id": self.incident_id, **entry})

    def set_state(self, state: str) -> None:
        if self.state == state:
            return
        self.state = state
        self.log("state", f"State -> {state}")
        bus.emit("incident_state", {"incident_id": self.incident_id, "state": state})

    # ------------------------------------------------------------------
    def set_hypotheses(self, hypotheses: list[dict[str, Any]]) -> None:
        self.hypotheses = hypotheses
        self.log("hypotheses", f"Recorded {len(hypotheses)} competing explanations",
                 {"hypotheses": hypotheses})

    def add_proposal(self, p: Proposal) -> None:
        self.proposals.append(p)
        self.set_state("response_proposed")
        self.log("proposal", f"Proposed {p.action}", p.to_dict())

    def add_test(self, r: TestReport) -> None:
        self.tests.append(r)
        self.set_state("validating")
        self.log("test", f"Candidate test {'passed' if r.passed else 'FAILED'}",
                 {"test_id": r.test_id, "passed": r.passed, "summary": r.summary,
                  "checks": r.checks})

    def add_execution(self, e: Execution) -> None:
        self.executions.append(e)
        self.set_state("applying")
        self.log("execution", f"Execution {e.outcome}", e.to_dict())

    def add_verification(self, v: VerificationResult) -> None:
        self.verifications.append(v)
        self.set_state("verifying")
        self.log("verification",
                 f"Verification {'passed' if v.passed else 'FAILED'}", v.to_dict())

    def add_monitoring(self, result: dict[str, Any]) -> None:
        self.monitoring.append(result)
        self.log("monitoring",
                 f"Post-recovery monitoring {'held' if result['passed'] else 'FAILED'}",
                 result)

    def note(self, text: str) -> None:
        if not text:
            return
        self.agent_narrative.append(text)
        self.log("narrative", text[:400])

    # ------------------------------------------------------------------
    def requires_approval(self, risk: str) -> bool:
        if settings.autonomy == "full":
            return False
        if settings.autonomy == "supervised":
            return True
        return RISK_ORDER.get(risk, 1) >= RISK_ORDER.get(
            settings.approval_risk_threshold, 2)

    def await_approval(self, proposal: Proposal) -> None:
        self.pending_approval = proposal
        self.log("approval_requested",
                 f"Human approval requested for {proposal.action}",
                 {"proposal_id": proposal.proposal_id, "risk": proposal.risk,
                  "changes": proposal.changes, "rationale": proposal.rationale})
        bus.emit("approval_requested", {
            "incident_id": self.incident_id, "proposal_id": proposal.proposal_id,
            "action": proposal.action, "changes": proposal.changes,
            "risk": proposal.risk, "rationale": proposal.rationale})

    def grant_approval(self, proposal_id: str) -> bool:
        if self.pending_approval and self.pending_approval.proposal_id == proposal_id:
            self.approval_granted = True
            self.pending_approval = None
            self.log("approval_granted", "Human approved the proposed response",
                     {"proposal_id": proposal_id})
            bus.emit("approval_granted", {"incident_id": self.incident_id,
                                          "proposal_id": proposal_id})
            return True
        return False

    def deny_approval(self, proposal_id: str, reason: str = "") -> bool:
        if self.pending_approval and self.pending_approval.proposal_id == proposal_id:
            self.pending_approval = None
            self.approval_granted = False
            self.log("approval_denied", "Human declined the proposed response",
                     {"proposal_id": proposal_id, "reason": reason})
            return True
        return False

    # ------------------------------------------------------------------
    @property
    def verified_ok(self) -> bool:
        return bool(self.verifications) and self.verifications[-1].passed

    @property
    def containment_applied(self) -> bool:
        return any(e.outcome == "applied"
                   and "Containment applied" in (e.detail or "")
                   for e in self.executions)

    def conclude(self, status: str, diagnosis: str, confidence: float,
                 evidence_ids: list[str], operator_note: str = "") -> dict[str, Any]:
        requested = status
        adjustments: list[str] = []
        if "conclude_invariants" in settings.ablate:
            # EVALUATION ONLY: let the narrative close the incident unchecked.
            self.status, self.diagnosis = status, diagnosis
            self.confidence = max(0.0, min(1.0, float(confidence)))
            self.cited_evidence = list(evidence_ids or [])
            self.operator_note, self.closed_at = operator_note, now()
            self.set_state(status)
            bus.emit("incident_closed", self.summary())
            return {"final_status": status, "requested_status": requested,
                    "adjustments": ["invariants ABLATED"], "detail": "Closed unchecked."}

        # Invariant 1 -- a recovered claim needs a passing independent check.
        if status in ("recovered", "recovered_under_containment") and not self.verified_ok:
            status = "needs_review"
            adjustments.append(
                "Downgraded: no passing verification exists, so a recovered state "
                "cannot be claimed (specification section 05).")

        # Invariant 2 -- low confidence cannot claim a repair.
        if status in ("recovered", "recovered_under_containment") and \
                confidence < settings.min_confidence_to_act:
            status = "needs_review"
            adjustments.append(
                f"Downgraded: stated confidence {confidence:.2f} is below the "
                f"{settings.min_confidence_to_act} threshold required to claim repair.")

        # Invariant 3 -- if a client was contained, say so in the end state.
        if status == "recovered" and self.containment_applied:
            status = "recovered_under_containment"
            adjustments.append(
                "Reclassified: a client was contained, so operation is recovered "
                "under containment.")

        self.status = status
        self.diagnosis = diagnosis
        self.confidence = max(0.0, min(1.0, float(confidence)))
        self.cited_evidence = list(evidence_ids or [])
        self.operator_note = operator_note
        self.closed_at = now()
        self.set_state(status)
        self.log("conclusion", f"Incident closed as {status}", {
            "requested_status": requested, "final_status": status,
            "adjustments": adjustments, "diagnosis": diagnosis,
            "confidence": self.confidence, "evidence_ids": self.cited_evidence,
            "operator_note": operator_note})
        bus.emit("incident_closed", self.summary())
        return {"final_status": status, "requested_status": requested,
                "adjustments": adjustments,
                "detail": "Incident closed." if not adjustments else
                          "Incident closed with adjustments enforced by the system."}

    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "state": self.state,
            "status": self.status,
            "detected_reasons": self.reasons,
            "diagnosis": self.diagnosis,
            "confidence": round(self.confidence, 3),
            "operator_note": self.operator_note,
            "cited_evidence": self.cited_evidence,
            "hypotheses": self.hypotheses,
            "proposals": [p.to_dict() for p in self.proposals],
            "tests": [t.to_dict() for t in self.tests],
            "executions": [e.to_dict() for e in self.executions],
            "verifications": [v.to_dict() for v in self.verifications],
            "monitoring": self.monitoring,
            "timeline": self.timeline,
            "narrative": self.agent_narrative,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "duration_s": round((self.closed_at or now()) - self.created_at, 2),
            "pending_approval": (self.pending_approval.to_dict()
                                 if self.pending_approval else None),
            "usage": self.usage,
            "error": self.error,
            "terminal": self.state in TERMINAL_STATES,
            "scenario_key": self.scenario_key,
        }
