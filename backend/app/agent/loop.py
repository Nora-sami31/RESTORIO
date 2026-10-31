"""The agentic controller: investigate -> diagnose -> act -> verify.

Bounded on four axes so it cannot spin: iterations, tool calls, applications
and wall-clock budget.  Hitting a bound is not a crash -- it produces an
escalation, which is a legitimate outcome.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from ..config import settings
from ..events import bus
from .brain import BrainError, make_brain
from .incident import Incident
from .prompt import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS, ToolBox

FALLBACK_TO_OFFLINE = os.environ.get(
    "RESTORIO_FALLBACK_OFFLINE", "false").strip().lower() in {"1", "true", "yes"}


def ends_the_run(tool_name: str, out: Any) -> bool:
    """Whether this tool result should end the incident.

    Only a conclude that was ACCEPTED ends the run. When the monitoring
    window rejects a recovery the tool returns an error inviting another
    investigation cycle -- stopping here would make that invitation
    meaningless and would close a station that is still broken. The
    iteration, tool call, application and time budgets still bound the
    loop, so it cannot spin.
    """
    if tool_name != "conclude":
        return False
    return not (isinstance(out, dict) and out.get("error"))


class AgentRunner:
    def __init__(self, engine, incident: Incident, provider: str | None = None) -> None:
        self.engine = engine
        self.incident = incident
        self.provider = provider or settings.resolved_provider()
        self.tools = ToolBox(engine, incident)
        self.messages: list[dict[str, Any]] = []
        self.started = 0.0
        self.iterations = 0
        self._last_call = 0.0
        self.approval_event = asyncio.Event()
        incident.approval_event = self.approval_event      # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        self.started = time.time()
        self.incident.set_state("investigating")
        bus.emit("agent_started", {"incident_id": self.incident.incident_id,
                                   "provider": self.provider})

        try:
            brain = make_brain(self.provider)
        except BrainError as exc:
            return self._model_unavailable(str(exc))

        self.messages = [{"role": "user", "content": self._opening_message()}]

        while True:
            if self.incident.state in ("recovered", "recovered_under_containment",
                                       "needs_review", "safe_state_unresolved"):
                break

            over, why = self._over_budget()
            if over:
                self._force_escalation(why)
                break

            self.iterations += 1
            await self._pace()
            try:
                reply = await asyncio.to_thread(
                    brain.converse, SYSTEM_PROMPT, self.messages, TOOL_SCHEMAS)
            except BrainError as exc:
                handled = await self._handle_model_error(exc)
                if handled is None:
                    break
                brain = handled
                continue
            except Exception as exc:                       # noqa: BLE001
                handled = await self._handle_model_error(BrainError(repr(exc)))
                if handled is None:
                    break
                brain = handled
                continue

            # Only count real model round-trips; the deterministic reasoner
            # makes none, and the evaluation table must say so honestly.
            if getattr(brain, "is_model", True):
                self.incident.usage["llm_calls"] += 1
                for k in ("input_tokens", "output_tokens"):
                    self.incident.usage[k] += reply.usage.get(k, 0)

            if reply.text:
                self.incident.note(reply.text.strip())
                bus.emit("agent_message", {"incident_id": self.incident.incident_id,
                                           "text": reply.text.strip()})

            if not reply.tool_calls:
                # The model stopped without acting. Nudge once, then escalate.
                if self.iterations >= 2 and not self.incident.proposals:
                    self._force_escalation(
                        "The agent stopped without proposing or testing a response.")
                    break
                self.messages.append(brain.assistant_turn(reply))
                self.messages.append({
                    "role": "user",
                    "content": "Continue. Use a tool, or call conclude if you are done."})
                continue

            self.messages.append(brain.assistant_turn(reply))

            results: list[tuple[str, Any]] = []
            terminal = False
            for tc in reply.tool_calls:
                out = await self.tools.call(tc.name, tc.arguments)

                # Human-in-the-loop pause.
                if isinstance(out, dict) and out.get("status") == "awaiting_human_approval":
                    granted = await self._wait_for_approval()
                    if granted:
                        out = await self.tools.call(tc.name, tc.arguments)
                    else:
                        out = {"status": "approval_not_granted",
                               "detail": "A human did not approve this response within "
                                         "the incident budget. Do not apply it. "
                                         "Conclude with needs_review."}
                results.append((tc.id, out))
                if ends_the_run(tc.name, out):
                    terminal = True

            turn = brain.tool_results_turn(results)
            if isinstance(turn, list):
                self.messages.extend(turn)
            else:
                self.messages.append(turn)

            if terminal:
                break

        return self._finish()

    # ------------------------------------------------------------------
    async def _pace(self) -> None:
        """Space model calls to fit a provider's requests-per-minute cap.

        Waiting here rather than absorbing a 429 keeps the request budget for
        useful work. The simulator keeps running throughout -- this is an
        await, not a block -- so the plant and the website are unaffected.
        """
        gap = settings.llm_min_interval_s
        if gap <= 0:
            return
        if self._last_call:
            wait = gap - (time.time() - self._last_call)
            if wait > 0:
                bus.emit("agent_pacing", {"incident_id": self.incident.incident_id,
                                          "waiting_s": round(wait, 1)})
                await asyncio.sleep(wait)
        self._last_call = time.time()

    def _opening_message(self) -> str:
        s = self.engine.sample()
        reasons = "\n".join(f"  - {r}" for r in self.incident.reasons)
        return (
            f"Incident {self.incident.incident_id} has been raised at station 1.\n\n"
            f"The monitor triggered because:\n{reasons}\n\n"
            f"Controller is in '{s['controller_mode']}' mode on feedback source "
            f"{s['feedback_source']}, setpoint {s['setpoint_bar']} bar, pump at "
            f"{s['pump_output_pct']}%. Configuration revision {s['config_revision']}."
            + (f"\nThe protective trip is ACTIVE." if s["tripped"] else "")
            + "\n\nInvestigate and recover the station."
        )

    def _over_budget(self) -> tuple[bool, str]:
        if self.iterations >= settings.max_iterations:
            return True, (f"Reached the {settings.max_iterations}-step investigation "
                          "limit without a confirmed recovery.")
        if self.tools.calls >= settings.max_tool_calls:
            return True, (f"Reached the {settings.max_tool_calls}-tool-call limit "
                          "without a confirmed recovery.")
        if time.time() - self.started >= settings.incident_budget_s:
            return True, (f"Reached the {settings.incident_budget_s:.0f}s incident "
                          "time budget without a confirmed recovery.")
        return False, ""

    def _force_escalation(self, why: str) -> None:
        """A bound was hit. Escalate honestly instead of retrying forever."""
        bus.emit("agent_escalation", {"incident_id": self.incident.incident_id,
                                      "reason": why})
        self.incident.conclude(
            status="needs_review",
            diagnosis=f"Investigation stopped before a confirmed recovery. {why}",
            confidence=0.3, evidence_ids=[],
            operator_note="Engineer review required. The agent reached a hard limit "
                          "and escalated rather than continuing to retry.")

    async def _wait_for_approval(self) -> bool:
        self.approval_event.clear()
        remaining = max(5.0, settings.incident_budget_s - (time.time() - self.started))
        try:
            await asyncio.wait_for(self.approval_event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return False
        return bool(self.incident.approval_granted)

    async def _handle_model_error(self, exc: BrainError):
        """Specification section 10: a model failure must not fake a recovery."""
        self.incident.error = str(exc)[:400]
        bus.emit("agent_model_error", {"incident_id": self.incident.incident_id,
                                       "error": str(exc)[:300]})
        self.incident.log("model_error", "Model provider failed", {"error": str(exc)[:300]})

        if FALLBACK_TO_OFFLINE:
            from .offline import OfflineBrain
            self.incident.note(
                "Model provider unavailable; continuing with the deterministic "
                "reasoner. This is recorded on the incident.")
            self.provider = "offline-fallback"
            return OfflineBrain()

        self.incident.conclude(
            status="needs_review",
            diagnosis="The model provider was unavailable, so no evidence-based "
                      "response could be selected. Local control and protective "
                      "behaviour continued unaffected.",
            confidence=0.0, evidence_ids=[],
            operator_note=f"Model error: {str(exc)[:200]}. The station kept running "
                          "under its deterministic controller; no configuration was "
                          "changed.")
        return None

    def _model_unavailable(self, detail: str) -> dict[str, Any]:
        self.incident.error = detail[:400]
        self.incident.conclude(
            status="needs_review",
            diagnosis="No model provider is configured or reachable; the incident "
                      "was not investigated automatically.",
            confidence=0.0, evidence_ids=[],
            operator_note=f"{detail[:200]} Set a provider key or run with "
                          "RESTORIO_PROVIDER=offline.")
        return self._finish()

    def _finish(self) -> dict[str, Any]:
        if self.incident.status is None:
            self._force_escalation("The investigation ended without a conclusion.")
        summary = self.incident.summary()
        summary["provider"] = self.provider
        summary["iterations"] = self.iterations
        summary["tool_calls"] = self.tools.calls
        summary["wall_s"] = round(time.time() - self.started, 2)
        bus.emit("agent_finished", summary)
        return summary
