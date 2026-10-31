"""Record shapes shared by the simulator, the agent and the audit trail.

Specification section 08 ("Minimum records").  Every one of these carries the
identifiers needed to click an asset in the UI and land on the exact event.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Quality flags carried with every measurement.  The agent is expected to
# check these rather than trusting a bare number.
# ---------------------------------------------------------------------------
QUALITY_GOOD = "good"
QUALITY_UNCERTAIN = "uncertain"
QUALITY_BAD = "bad"
QUALITY_STALE = "stale"


@dataclass
class Measurement:
    asset_id: str
    source_id: str          # which path produced it (local / forwarded / trusted)
    timestamp: float
    value: float | None
    unit: str
    quality: str
    config_revision: int
    raw_signal_ma: float | None = None
    age_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceItem:
    """A document excerpt or a log event.  Treated as data, never instruction."""
    evidence_id: str
    source_id: str
    kind: str               # work_order | spec | config_snapshot | access_event | note
    timestamp: float
    asset_ids: list[str]
    title: str
    excerpt: str
    provenance: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hypothesis:
    name: str
    stance: str                       # supported | contradicted | unresolved
    rationale: str
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Proposal:
    proposal_id: str
    incident_id: str
    action: str
    target: str
    changes: dict[str, Any]
    evidence_ids: list[str]
    rationale: str
    expected_checks: list[str]
    bound_revision: int               # rejected later if the plant moved on
    risk: str = "medium"              # low | medium | high
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestReport:
    test_id: str
    proposal_id: str
    passed: bool
    bound_revision: int
    checks: list[dict[str, Any]]
    summary: str
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Execution:
    execution_id: str
    proposal_id: str
    test_id: str | None
    prior_revision: int
    new_revision: int
    applied_at: float
    outcome: str                      # applied | rejected | failed
    detail: str
    affected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationResult:
    verification_id: str
    incident_id: str
    passed: bool
    checks: list[dict[str, Any]]
    summary: str
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Incident lifecycle -- specification section 05.
INCIDENT_STATES = [
    "detected",
    "investigating",
    "response_proposed",
    "validating",
    "applying",
    "verifying",
    "monitoring",
    # terminal
    "recovered",
    "recovered_under_containment",
    "needs_review",
    "safe_state_unresolved",
]

TERMINAL_STATES = {
    "recovered",
    "recovered_under_containment",
    "needs_review",
    "safe_state_unresolved",
}
