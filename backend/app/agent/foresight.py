"""Agentic forecasting over the recorded incident history.

The rest of RESTORIO reasons about what is wrong *now*.  This module reasons
about what is likely to go wrong *next*, and it is built the same way the
investigation loop is built: narrow read-only tools supply facts, the model
decides which facts to ask for and what they mean, and deterministic code
checks the resulting claim before it reaches an operator.

Why the model and not a formula.  ``store.forecast`` already computes the
statistics -- recurrence counts, intervals, escalation rates, a least-squares
slope on commanded pump output.  Those are facts, and a formula can produce
them.  What a formula cannot do is decide which of eight fault classes matters
this shift, tie a recurrence to a physical mechanism, or notice that a periodic
model has stopped describing a class.  That is judgement, so the model makes
it.

Why the model is still fenced.  A forecast writes nothing to the plant, so it
needs no validator and no executor -- the model's conclusion is the product.
But an unsupported forecast is its own harm: it sends a crew to the wrong
asset.  So the claim check below rejects a forecast whose basis is not in the
record it was given, and hands the model the reason so it can revise.

The model never sees the scenario label or the injected-fault list here either.
It sees the same closed-incident statistics an operator could read.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..config import settings
from ..store import forecast as stats
from .brain import BrainError, ToolCall, make_brain

MAX_ITERATIONS = 5
MAX_TOOL_CALLS = 8

SYSTEM = """You are a reliability analyst for an industrial pumping station.

You are not diagnosing a live fault. You are answering one question from the
station's recorded incident history: what is most likely to go wrong next, and
what should the operator watch for?

Tools available to you:
  read_incident_history  -- recurrence statistics over closed incidents,
                            including the assets each class was recorded against
  read_live_trend        -- a measured slope on the live control signals
  record_forecast        -- your conclusion (ends your turn)

Rules you must follow:

1. Call read_incident_history before you record anything. You may not forecast
   from memory or assumption.
2. Every number in your forecast must come from a tool result. Do not invent
   counts, intervals or rates.
3. Name the fault classes you relied on in scenario_keys. A class with fewer
   than 3 recorded occurrences is too thin to carry a recurrence forecast; you
   may mention it as a watch item but not as the basis of the claim.
4. If a class carries interval_model_holds = false, its occurrences are not
   periodic. Do not call it "overdue". Say the pattern exists but its timing is
   not predictable from this record.
5. Keep the horizon inside the recorded span (observed_span_h). If the evidence
   only covers hours, do not forecast days.
6. Take the asset from the "assets" field of the class you are relying on. Do
   not guess an equipment tag. If a class names no asset, say so rather than
   inventing one.
7. Prefer the physical mechanism over the label. "Commanded output is climbing
   and will saturate" is useful; "pump_degraded may recur" is not.

Be direct and specific. An operator reads this at shift handover."""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_incident_history",
        "description": ("Recurrence statistics over closed incidents: per fault "
                        "class the occurrence count, outcomes, escalation rate, "
                        "mean interval, time since last, the assets the class "
                        "was recorded against, and whether a periodic model "
                        "still describes it."),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "how many incidents to read (default all)"},
            },
        },
    },
    {
        "name": "read_live_trend",
        "description": ("A least-squares slope over the live control signals, with "
                        "the fit quality, and time-to-saturation for commanded pump "
                        "output. Reports when a window does not fit a line."),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_s": {"type": "number",
                             "description": "seconds of history to fit (default 120)"},
            },
        },
    },
    {
        "name": "record_forecast",
        "description": "Record your forecast. This ends your turn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string",
                          "description": "one sentence: what is likely to happen"},
                "asset": {"type": "string",
                          "description": ("the asset most at risk, taken from the "
                                          "assets field of the class you relied "
                                          "on -- not guessed")},
                "mechanism": {"type": "string",
                              "description": "the physical reason, in one sentence"},
                "horizon_h": {"type": "number",
                              "description": "hours ahead this covers"},
                "confidence": {"type": "number",
                               "description": "0.0 to 1.0"},
                "basis": {"type": "string",
                          "description": ("the counts and intervals you relied on, "
                                          "quoted from the tool results")},
                "scenario_keys": {"type": "array", "items": {"type": "string"},
                                  "description": "fault classes the claim rests on"},
                "watch": {"type": "array", "items": {"type": "string"},
                          "description": "what the operator should watch, 1-3 items"},
            },
            "required": ["claim", "asset", "mechanism", "horizon_h",
                         "confidence", "basis", "scenario_keys"],
        },
    },
]


# ---------------------------------------------------------------------------
# claim check -- deterministic, runs on the model's conclusion
# ---------------------------------------------------------------------------
def check_claim(args: dict[str, Any], hist: dict[str, Any]) -> list[dict[str, Any]]:
    """Test a recorded forecast against the record it was supposed to rest on.

    Returns a list of checks in the same shape the validator uses elsewhere, so
    the UI and the audit trail can render it the same way.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    by_key = {c["scenario_key"]: c for c in (hist.get("classes") or [])}
    keys = [str(k) for k in (args.get("scenario_keys") or [])]

    add("history_was_read", bool(hist.get("available")),
        "recurrence statistics were available to the forecast"
        if hist.get("available") else "no recorded history was read")

    known = [k for k in keys if k in by_key]
    add("classes_exist_in_record", bool(keys) and len(known) == len(keys),
        f"cited classes present in the record: {known}" if known
        else f"cited classes not found in the record: {keys}")

    # The basis of a recurrence claim needs enough occurrences to be a pattern.
    thick = [k for k in known if by_key[k]["n"] >= stats.HEADLINE_MIN_N]
    add("basis_thick_enough", bool(thick),
        (f"{thick} each have {stats.HEADLINE_MIN_N}+ recorded occurrences")
        if thick else
        (f"no cited class reaches {stats.HEADLINE_MIN_N} occurrences; "
         "too thin to carry a recurrence forecast"))

    # A horizon longer than the record is arithmetic, not evidence.
    span_h = float(hist.get("observed_span_h") or 0.0)
    horizon = float(args.get("horizon_h") or 0.0)
    add("horizon_within_record", horizon > 0 and horizon <= max(span_h, 0.1),
        f"horizon {horizon:.2f} h within {span_h:.2f} h of recorded history"
        if horizon <= span_h else
        f"horizon {horizon:.2f} h exceeds the {span_h:.2f} h of recorded history")

    # An asset tag that appears in no recorded diagnosis is a guess.
    asset = str(args.get("asset") or "").strip()
    recorded = {a for k in known for a in (by_key[k].get("assets") or [])}
    add("asset_in_record", (not asset) or (not recorded) or asset in recorded,
        f"{asset} appears in the recorded diagnoses for {known}" if asset in recorded
        else (f"no asset claimed; cited classes name {sorted(recorded)}" if not asset
              else f"{asset} does not appear in the record for {known}: "
                   f"{sorted(recorded)}"))

    conf = float(args.get("confidence") or 0.0)
    add("confidence_in_range", 0.0 < conf <= 1.0, f"confidence {conf}")

    return checks


class ForecastAgent:
    """A short agentic loop that produces one forecast."""

    def __init__(self, engine: Any, rows: list[dict[str, Any]],
                 now_ts: float | None = None) -> None:
        self.engine = engine
        self.rows = rows
        self.now_ts = now_ts if now_ts is not None else time.time()
        self.hist: dict[str, Any] = {}
        self.live: dict[str, Any] = {}
        self.trail: list[dict[str, Any]] = []
        self.usage: dict[str, int] = {}

    # -- tools ------------------------------------------------------------
    def _read_incident_history(self, limit: int | None = None) -> dict[str, Any]:
        rows = self.rows[-limit:] if limit else self.rows
        self.hist = stats.recurrence(rows, self.now_ts)
        return self.hist

    def _read_live_trend(self, window_s: float = 120.0) -> dict[str, Any]:
        self.live = stats.project_live(self.engine, window_s)
        return self.live

    def _dispatch(self, tc: ToolCall) -> Any:
        a = tc.arguments or {}
        if tc.name == "read_incident_history":
            return self._read_incident_history(a.get("limit"))
        if tc.name == "read_live_trend":
            return self._read_live_trend(float(a.get("window_s") or 120.0))
        return {"error": f"unknown tool '{tc.name}'"}

    # -- loop -------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        brain = make_brain()
        provider = settings.provider
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": ("Produce the shift-handover forecast for this station. "
                        "Read the recorded history first."),
        }]
        calls = 0

        for _ in range(MAX_ITERATIONS):
            try:
                reply = brain.converse(SYSTEM, messages, TOOL_SCHEMAS)
            except BrainError as exc:
                return self._fail(f"provider error: {exc}", provider)

            for k, v in (reply.usage or {}).items():
                if isinstance(v, int):
                    self.usage[k] = self.usage.get(k, 0) + v

            if not reply.tool_calls:
                # The model answered in prose without recording. Push it once.
                messages.append(brain.assistant_turn(reply))
                messages.append({"role": "user", "content":
                                 "Record the forecast with record_forecast."})
                continue

            messages.append(brain.assistant_turn(reply))
            results: list[tuple[str, Any]] = []

            for tc in reply.tool_calls:
                calls += 1
                if tc.name == "record_forecast":
                    args = tc.arguments or {}
                    checks = check_claim(args, self.hist)
                    failed = [c for c in checks if not c["passed"]]
                    self.trail.append({"tool": tc.name,
                                       "accepted": not failed,
                                       "checks": checks})
                    if not failed:
                        return self._ok(args, checks, provider)
                    # Hand the reason back and let the model revise.
                    results.append((tc.id, {
                        "error": "forecast_rejected",
                        "failed_checks": [{"name": c["name"],
                                           "detail": c["detail"]} for c in failed],
                        "hint": ("Revise using only classes present in the "
                                 "record with enough occurrences, and keep the "
                                 "horizon inside the recorded span."),
                    }))
                    continue

                out = self._dispatch(tc)
                self.trail.append({"tool": tc.name,
                                   "arguments": tc.arguments or {},
                                   "summary": _summarise(tc.name, out)})
                results.append((tc.id, out))

                if calls >= MAX_TOOL_CALLS:
                    break

            turn = brain.tool_results_turn(results)
            if isinstance(turn, list):
                messages.extend(turn)
            else:
                messages.append(turn)

            if calls >= MAX_TOOL_CALLS:
                return self._fail("tool-call budget reached without a forecast",
                                  provider)

        return self._fail("iteration budget reached without a forecast", provider)

    # -- results ----------------------------------------------------------
    def _ok(self, args: dict[str, Any], checks: list[dict[str, Any]],
            provider: str) -> dict[str, Any]:
        return {
            "available": True,
            "agentic": True,
            "provider": provider,
            "claim": args.get("claim", ""),
            "asset": args.get("asset", ""),
            "mechanism": args.get("mechanism", ""),
            "horizon_h": args.get("horizon_h"),
            "confidence": args.get("confidence"),
            "basis": args.get("basis", ""),
            "scenario_keys": args.get("scenario_keys") or [],
            "watch": args.get("watch") or [],
            "checks": checks,
            "tool_trail": self.trail,
            "usage": self.usage,
            "history": self.hist,
            "live": self.live,
            "generated_at": self.now_ts,
        }

    def _fail(self, reason: str, provider: str) -> dict[str, Any]:
        return {"available": False, "agentic": True, "provider": provider,
                "reason": reason, "tool_trail": self.trail,
                "usage": self.usage, "history": self.hist, "live": self.live,
                "generated_at": self.now_ts}


def _summarise(name: str, out: Any) -> str:
    """A one-line record of what a tool returned, for the UI trail."""
    if not isinstance(out, dict):
        return str(out)[:160]
    if name == "read_incident_history":
        if not out.get("available"):
            return "no recorded history"
        return (f"{out['incidents']} incidents over {out['observed_span_h']} h, "
                f"{len(out.get('classes') or [])} fault classes")
    if name == "read_live_trend":
        if not out.get("available"):
            return str(out.get("reason") or "unavailable")
        if out.get("projectable"):
            return out.get("detail") or "projectable"
        return str(out.get("reason") or "not projectable")
    return json.dumps(out)[:160]


# ---------------------------------------------------------------------------
# offline baseline
# ---------------------------------------------------------------------------
def offline_forecast(engine: Any, rows: list[dict[str, Any]],
                     now_ts: float | None = None) -> dict[str, Any]:
    """A rules forecast for the no-key demonstration.

    This is the same deal the rest of the system offers offline: a repeatable
    baseline that needs no API key, reported as rules rather than dressed up as
    reasoning.  It composes the statistics into a sentence; it does not choose
    what to read or decide what matters, so ``agentic`` is false.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    hist = stats.recurrence(rows, now_ts)
    live = stats.project_live(engine, 120.0)

    base = {"available": False, "agentic": False, "provider": "offline",
            "tool_trail": [], "usage": {}, "history": hist, "live": live,
            "generated_at": now_ts}

    h = hist.get("headline")
    if not h:
        return base | {"reason": ("no fault class has reached "
                                  f"{stats.HEADLINE_MIN_N} recorded occurrences")}

    key = h["scenario_key"]
    span_h = float(hist.get("observed_span_h") or 0.0)
    interval_h = (h["mean_interval_s"] or 0.0) / 3600.0
    pressure = key in stats.PRESSURE_LOSS_CLASSES

    if h["interval_model_holds"] and h["due_in_s"] is not None:
        due_h = h["due_in_s"] / 3600.0
        timing = (f"due in about {due_h:.1f} h on its {interval_h:.1f} h "
                  "recorded interval")
    else:
        timing = (f"recurs on a {interval_h:.1f} h mean interval, but the last "
                  f"gap is {h['elapsed_over_interval']}x that, so the timing is "
                  "not predictable from this record")

    subject = ("loss of discharge pressure at P-101" if pressure
               else f"recurrence of {key.replace('_', ' ')}")
    claim = (f"Most likely next escalation is {subject}: "
             f"{h['escalated']} of {h['n']} recorded occurrences escalated "
             f"rather than recovering in software, and it {timing}.")

    mechanism = ("Impeller wear reduces the pump's achievable head, so the "
                 "controller raises commanded output to hold setpoint until it "
                 "saturates, after which discharge pressure falls."
                 if pressure else
                 f"Recorded outcomes for {key} are {h['outcomes']}.")

    watch = ["commanded pump output trending toward 100%",
             "setpoint-minus-feedback error widening"]
    if live.get("projectable") and not live.get("saturated"):
        watch.insert(0, live.get("detail") or "")

    assets = h.get("assets") or []
    args = {"claim": claim, "asset": assets[0] if assets else "",
            "mechanism": mechanism,
            "horizon_h": round(min(max(interval_h, 0.5), max(span_h, 0.5)), 2),
            "confidence": 0.55 if h["interval_model_holds"] else 0.4,
            "basis": (f"{h['n']} recorded occurrences of {key}, "
                      f"{h['escalated']} escalated, mean interval "
                      f"{interval_h:.2f} h, over {span_h:.2f} h of history"),
            "scenario_keys": [key], "watch": [w for w in watch if w]}

    checks = check_claim(args, hist)
    return base | {"available": all(c["passed"] for c in checks),
                   "checks": checks, **args}
