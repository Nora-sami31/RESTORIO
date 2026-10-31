"""Forward-looking risk analysis over the recorded incident history.

Specification section 07 ("Audit + evaluation") leaves us a durable record of
every closed incident.  This module reads that record to answer a different
question from the rest of the system: not "what is wrong now" but "what has
gone wrong repeatedly, and is it due again".

Three honesty rules shape everything here, because a forecast is the easiest
place in a system like this to produce a confident number from nothing:

  * Nothing is projected from a single observation.  A fault class seen once
    has no interval, and the record says so rather than inventing one.
  * Every projection carries the count it was computed from, and a projection
    that runs past the end of the recorded history is flagged.  Thirteen hours
    of operating record cannot support a claim about next week.
  * The live projection extrapolates a measured slope and reports the fit.  A
    trend that does not fit a line is reported as not projectable.

The output is operator-facing analysis.  It is deliberately *not* admissible
as evidence for an intervention: the evidence gate requires records from the
plant, not statistics about the plant's past.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

ESCALATED = {"needs_review", "safe_state_unresolved"}
CONTAINED = {"recovered_under_containment"}
CLEAN = {"recovered"}

# A fault class needs this many occurrences before it is named in a headline.
# Two occurrences give one interval, which is a coincidence about as often as
# it is a pattern.
HEADLINE_MIN_N = 3

# Commanded pump output is a percentage; once it saturates the pump has no
# authority left and discharge pressure falls regardless of the setpoint.
OUTPUT_CEILING_PCT = 100.0

# Below this coefficient of determination the window is not behaving like a
# line and we decline to extrapolate it.
MIN_FIT_R2 = 0.80

# When the time since the last occurrence exceeds the mean interval by more
# than this factor, the class is not recurring on that interval and saying
# "overdue" would be a louder claim than the data supports.
MODEL_HOLDS_RATIO = 3.0

# Asset tags as they appear in recorded diagnoses (PT-101, P-101, PLC-101...).
# The forecast reports the assets a class actually named rather than letting
# the model guess which equipment a fault class belongs to.
_ASSET_TAG = re.compile(r"\b(?:PT|PLC|GW|FT|TT|P)-\d+\b")


# Fault classes whose mechanism is a loss of discharge pressure.  Used only to
# phrase the headline in process terms; the statistics are class-agnostic.
PRESSURE_LOSS_CLASSES = {"pump_degraded", "blockage", "h5_sensor_drift"}


def _linfit(pts: list[tuple[float, float]]) -> dict[str, float] | None:
    """Least-squares slope of ``pts``, with the fit quality that justifies it.

    Returns ``None`` when there is nothing to fit (fewer than three points, or
    no spread on the time axis).
    """
    if len(pts) < 3:
        return None
    n = float(len(pts))
    sx = sum(t for t, _ in pts)
    sy = sum(v for _, v in pts)
    mx, my = sx / n, sy / n
    sxx = sum((t - mx) ** 2 for t, _ in pts)
    if sxx <= 0:
        return None
    sxy = sum((t - mx) * (v - my) for t, v in pts)
    slope = sxy / sxx
    intercept = my - slope * mx
    # Coefficient of determination.  A flat series is a perfect fit to a flat
    # line, which is the honest reading: no trend, and no projection from it.
    sst = sum((v - my) ** 2 for _, v in pts)
    if sst <= 0:
        r2 = 1.0
    else:
        ssr = sum((v - (slope * t + intercept)) ** 2 for t, v in pts)
        r2 = max(0.0, 1.0 - ssr / sst)
    return {"slope": slope, "intercept": intercept, "r2": r2}


def _basis(n: int) -> str:
    """How much weight the occurrence count can carry."""
    if n >= 4:
        return "pattern"
    if n == 3:
        return "provisional"
    if n == 2:
        return "one interval"
    return "single occurrence"


def recurrence(rows: Iterable[dict[str, Any]], now_ts: float) -> dict[str, Any]:
    """Per-fault-class recurrence statistics from the closed-incident record.

    ``rows`` carry ``scenario_key``, ``status`` and ``created_at``.  The result
    ranks classes by observed escalations per hour of recorded history, which
    is a measured rate rather than a weighting we chose.
    """
    rows = [r for r in rows if r.get("scenario_key") and r.get("created_at")]
    if not rows:
        return {"available": False, "reason": "no closed incidents recorded",
                "incidents": 0, "classes": [], "headline": None}

    stamps = sorted(float(r["created_at"]) for r in rows)
    span_s = stamps[-1] - stamps[0]
    span_h = span_s / 3600.0

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(str(r["scenario_key"]), []).append(r)

    classes: list[dict[str, Any]] = []
    for key, items in groups.items():
        items.sort(key=lambda r: float(r["created_at"]))
        ts = [float(r["created_at"]) for r in items]
        n = len(items)
        outcomes: dict[str, int] = {}
        for r in items:
            s = str(r.get("status") or "open")
            outcomes[s] = outcomes.get(s, 0) + 1
        escalated = sum(v for k, v in outcomes.items() if k in ESCALATED)

        # Which equipment this class has actually been recorded against.
        tally: dict[str, int] = {}
        for r in items:
            for tag in set(_ASSET_TAG.findall(str(r.get("diagnosis") or ""))):
                tally[tag] = tally.get(tag, 0) + 1
        assets = sorted(tally, key=lambda a: (-tally[a], a))

        gaps = [b - a for a, b in zip(ts, ts[1:])]
        mean_gap = (sum(gaps) / len(gaps)) if gaps else None
        if len(gaps) > 1:
            m = mean_gap or 0.0
            stdev = (sum((g - m) ** 2 for g in gaps) / len(gaps)) ** 0.5
        else:
            stdev = None

        elapsed = now_ts - ts[-1]
        due_in = (mean_gap - elapsed) if mean_gap is not None else None

        # Does a periodic model describe this class at all?  If the gap since
        # the last occurrence dwarfs the mean gap between occurrences, it does
        # not, and the projection is reported as not holding instead of as an
        # alarming negative number.
        ratio = (elapsed / mean_gap) if (mean_gap and mean_gap > 0) else None
        model_holds = bool(ratio is not None and ratio <= MODEL_HOLDS_RATIO)

        classes.append({
            "scenario_key": key,
            "n": n,
            "basis": _basis(n),
            "assets": assets,
            "asset_mentions": tally,
            "outcomes": outcomes,
            "escalated": escalated,
            "escalation_rate": round(escalated / n, 3),
            "contained": sum(v for k, v in outcomes.items() if k in CONTAINED),
            "clean": sum(v for k, v in outcomes.items() if k in CLEAN),
            "first_seen": ts[0],
            "last_seen": ts[-1],
            "intervals": len(gaps),
            "mean_interval_s": round(mean_gap, 1) if mean_gap is not None else None,
            "interval_stdev_s": round(stdev, 1) if stdev is not None else None,
            "elapsed_since_last_s": round(elapsed, 1),
            "due_in_s": round(due_in, 1) if due_in is not None else None,
            "overdue": bool(due_in is not None and due_in <= 0),
            "elapsed_over_interval": round(ratio, 2) if ratio is not None else None,
            "interval_model_holds": model_holds,
            # A projection longer than the whole record is arithmetic, not
            # evidence.  Say which it is.
            "beyond_observed_span": bool(due_in is not None and due_in > span_s),
            "escalations_per_h": round(escalated / span_h, 3) if span_h > 0 else None,
            "headline_eligible": n >= HEADLINE_MIN_N,
        })

    classes.sort(key=lambda c: (-(c["escalations_per_h"] or 0.0), -c["n"]))

    # Prefer a class the periodic model still describes; fall back to the
    # ranked escalator, which then carries its own "model does not hold" flag.
    eligible = [c for c in classes
                if c["headline_eligible"] and c["escalated"] > 0]
    headline = next((c for c in eligible if c["interval_model_holds"]), None)
    if headline is None:
        headline = eligible[0] if eligible else None

    return {
        "available": True,
        "incidents": len(rows),
        "observed_span_s": round(span_s, 1),
        "observed_span_h": round(span_h, 2),
        "window_first": stamps[0],
        "window_last": stamps[-1],
        "classes": classes,
        "headline": headline,
        "headline_min_n": HEADLINE_MIN_N,
    }


def project_live(engine: Any, window_s: float = 120.0) -> dict[str, Any]:
    """Short-horizon projection from the live metric series.

    The leading indicator of pressure loss is not pressure -- a working
    controller holds pressure while it still has authority.  It is commanded
    pump output climbing to hold the setpoint.  Once output saturates the
    controller has nothing left and discharge pressure falls, so the honest
    projection is time-to-saturation from the measured slope.
    """
    out: dict[str, Any] = {"available": False, "window_s": window_s}
    try:
        now_t = engine.plant.sim_time
        m = engine.metrics.read("pump.output_pct", window_s, now_t)
        err = engine.metrics.read("process.pressure_error_bar", window_s, now_t)
    except Exception:
        return out | {"reason": "live metrics unavailable"}

    pts = [(float(t), float(v)) for t, v in (m.get("points") or [])]
    fit = _linfit(pts)
    if not fit:
        return out | {"reason": "not enough samples in the window"}

    latest = pts[-1][1]
    slope = fit["slope"]            # percent per second
    r2 = fit["r2"]
    res: dict[str, Any] = {
        "available": True,
        "window_s": window_s,
        "metric": "pump.output_pct",
        "latest_pct": round(latest, 2),
        "slope_pct_per_s": round(slope, 4),
        "slope_pct_per_min": round(slope * 60.0, 3),
        "fit_r2": round(r2, 3),
        "samples": len(pts),
        "ceiling_pct": OUTPUT_CEILING_PCT,
        "pressure_error_bar": err.get("latest"),
        "pressure_error_trend_bar": err.get("trend"),
    }

    if r2 < MIN_FIT_R2:
        return res | {"projectable": False,
                      "reason": f"window does not fit a line (R²={r2:.2f})"}
    if slope <= 0:
        return res | {"projectable": False,
                      "reason": "commanded output is not climbing"}

    remaining = OUTPUT_CEILING_PCT - latest
    if remaining <= 0:
        return res | {"projectable": True, "saturated": True, "eta_s": 0.0,
                      "detail": "commanded output is already at its ceiling; "
                                "the controller has no authority left"}

    eta = remaining / slope
    return res | {
        "projectable": True,
        "saturated": False,
        "eta_s": round(eta, 1),
        "eta_min": round(eta / 60.0, 2),
        # Extrapolating many multiples of the observed window is arithmetic.
        "beyond_observed_window": bool(eta > window_s * 5),
        "detail": (f"commanded output rising {slope * 60.0:.2f} %/min "
                   f"(R²={r2:.2f}); reaches {OUTPUT_CEILING_PCT:.0f}% in "
                   f"~{eta / 60.0:.1f} min, after which discharge pressure "
                   f"falls because the controller has no authority left"),
    }


def forecast(rows: Iterable[dict[str, Any]], engine: Any,
             now_ts: float, window_s: float = 120.0) -> dict[str, Any]:
    """Recorded-history recurrence plus the live short-horizon projection."""
    hist = recurrence(rows, now_ts)
    live = project_live(engine, window_s) if engine is not None else {
        "available": False, "reason": "no live engine"}
    return {"history": hist, "live": live,
            "note": ("Operator analysis computed from recorded incidents and "
                     "live metrics. Not admissible as evidence for an "
                     "intervention.")}
