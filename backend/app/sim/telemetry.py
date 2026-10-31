"""Logs and system metrics.

The challenge asks the agent to "check logs, metrics and system data". These
are those surfaces, and the rule from the project specification still holds:
**everything here is produced by the running system**. No log line and no
metric sample is hand-written to point at an answer. They are emitted from
plant and gateway state as it actually evolves, including the ordinary
background chatter the agent has to read past.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


@dataclass
class LogLine:
    ts: float
    sim_t: float
    source: str
    level: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"timestamp": self.ts, "sim_t": round(self.sim_t, 2),
                "source": self.source, "level": self.level,
                "message": self.message, "fields": self.fields}


class LogBook:
    """Structured log lines from PLC-101, the gateway and the historian."""

    def __init__(self, capacity: int = 4000) -> None:
        self.lines: deque[LogLine] = deque(maxlen=capacity)
        self._last_heartbeat = -999.0
        self._last_state: dict[str, Any] = {}
        self._stale_logged = False

    def write(self, sim_t: float, ts: float, source: str, level: str,
              message: str, **fields: Any) -> LogLine:
        ln = LogLine(ts=ts, sim_t=sim_t, source=source, level=level,
                     message=message, fields=fields)
        self.lines.append(ln)
        return ln

    # -- automatic emission from state transitions -----------------------
    def observe(self, sim_t: float, ts: float, state: dict[str, Any]) -> None:
        """Emit log lines for anything that changed since the last tick."""
        state = {**state, "_t": sim_t}
        prev, self._last_state = self._last_state, dict(state)
        if not prev:
            self.write(sim_t, ts, "PLC-101", "INFO",
                       "Controller started, automatic mode",
                       setpoint_bar=state.get("setpoint"),
                       feedback=state.get("feedback_source"))
            return

        def changed(key):
            return prev.get(key) != state.get(key)

        if changed("controller_mode"):
            lvl = "WARN" if state.get("controller_mode") == "safe_state" else "INFO"
            self.write(sim_t, ts, "PLC-101", lvl,
                       f"Controller mode {prev.get('controller_mode')} -> "
                       f"{state.get('controller_mode')}",
                       previous=prev.get("controller_mode"),
                       current=state.get("controller_mode"))

        if changed("feedback_source"):
            self.write(sim_t, ts, "PLC-101", "WARN",
                       f"Feedback source changed to {state.get('feedback_source')}",
                       previous=prev.get("feedback_source"),
                       current=state.get("feedback_source"))

        if changed("config_revision"):
            self.write(sim_t, ts, "PLC-101", "INFO",
                       f"Configuration revision {prev.get('config_revision')} -> "
                       f"{state.get('config_revision')} applied",
                       revision=state.get("config_revision"))

        if changed("pt102_quality"):
            q = state.get("pt102_quality")
            lvl = {"good": "INFO", "uncertain": "WARN"}.get(q, "ERROR")
            self.write(sim_t, ts, "PT-102", lvl,
                       f"Reference measurement quality is '{q}'", quality=q)

        if state.get("tripped") and not prev.get("tripped"):
            self.write(sim_t, ts, "PLC-101", "ERROR",
                       "Protective trip activated; pump commanded to minimum",
                       limit_bar=state.get("trip_limit"))

        # Saturated output is a genuine operational warning, and in the scaling
        # scenario it is the earliest honest hint that something is wrong.
        pump = state.get("pump_output_pct") or 0.0
        if pump >= 90.0 and (prev.get("pump_output_pct") or 0) < 90.0:
            self.write(sim_t, ts, "PLC-101", "WARN",
                       "Pump output above 90% sustained; controller cannot reach "
                       "setpoint on current feedback",
                       pump_output_pct=round(pump, 1),
                       setpoint_bar=state.get("setpoint"))

        # Report a stalled path whenever it is stalled and unreported, not
        # only on the transition -- a fault can already be present when the
        # log starts, which is exactly what happens on a restart.
        if state.get("forward_stale"):
            if not self._stale_logged:
                self._stale_logged = True
                self.write(sim_t, ts, "telemetry-gateway", "ERROR",
                           "Forwarding path not refreshing; downstream value is "
                           "held at its last delivered sample",
                           held_value_bar=state.get("forwarded"))
            elif int(sim_t) % 15 == 0 and int(sim_t) != int(prev.get("_t", -1)):
                self.write(sim_t, ts, "telemetry-gateway", "WARN",
                           "Forwarding retry backlog growing on the PT-101 path",
                           held_value_bar=state.get("forwarded"))
        else:
            self._stale_logged = False

        # Background chatter, so the log is not a list of answers.
        if sim_t - self._last_heartbeat >= 20.0:
            self._last_heartbeat = sim_t
            self.write(sim_t, ts, "historian", "INFO",
                       "Archive flush complete", records=int(sim_t * 4) % 997 + 120)
            self.write(sim_t, ts, "telemetry-gateway", "INFO",
                       "Session keepalive ok",
                       clients=state.get("client_count", 2))

    def query(self, source: str | None = None, level: str | None = None,
              contains: str | None = None, since_sim_t: float | None = None,
              limit: int = 60) -> list[dict[str, Any]]:
        out = []
        floor = _RANK.get((level or "DEBUG").upper(), 0)
        needle = (contains or "").lower()
        for ln in self.lines:
            if source and ln.source != source:
                continue
            if _RANK.get(ln.level, 0) < floor:
                continue
            if since_sim_t is not None and ln.sim_t < since_sim_t:
                continue
            if needle and needle not in f"{ln.message} {ln.fields}".lower():
                continue
            out.append(ln.to_dict())
        return out[-limit:]


class MetricStore:
    """Named time series, sampled every tick from real plant state."""

    DESCRIPTIONS = {
        "plc.scan_time_ms": "PLC-101 control loop scan time",
        "plc.scan_overruns_per_min": "scan cycles that exceeded their budget",
        "gateway.forward_rate_hz": "telemetry updates forwarded to the controller",
        "gateway.forward_latency_ms": "age of the value handed to the controller",
        "gateway.comms_retries_per_min": "retries on the PT-101 forwarding path",
        "gateway.cpu_pct": "telemetry gateway CPU utilisation",
        "pt101.update_rate_hz": "PT-101 sample rate at the transmitter",
        "pt102.update_rate_hz": "PT-102 reference sample rate",
        "pump.output_pct": "P-101 commanded output",
        "process.pressure_error_bar": "setpoint minus the controlling feedback",
    }

    def __init__(self, capacity: int = 2400) -> None:
        self.series: dict[str, deque] = {k: deque(maxlen=capacity)
                                         for k in self.DESCRIPTIONS}

    def record(self, sim_t: float, values: dict[str, float]) -> None:
        for k, v in values.items():
            if k in self.series and v is not None:
                self.series[k].append((round(sim_t, 2), round(float(v), 3)))

    def read(self, name: str, window_s: float = 60.0,
             now_sim_t: float = 0.0) -> dict[str, Any]:
        if name not in self.series:
            return {"error": f"unknown metric '{name}'",
                    "available": sorted(self.series)}
        cutoff = now_sim_t - window_s
        pts = [(t, v) for t, v in self.series[name] if t >= cutoff]
        vals = [v for _t, v in pts]
        return {
            "metric": name, "description": self.DESCRIPTIONS[name],
            "window_s": window_s, "samples": len(pts),
            "latest": vals[-1] if vals else None,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "mean": round(sum(vals) / len(vals), 3) if vals else None,
            "first": vals[0] if vals else None,
            "trend": (round(vals[-1] - vals[0], 3) if len(vals) > 1 else 0.0),
            "points": pts[-40:],
        }

    def summary(self, window_s: float, now_sim_t: float) -> list[dict[str, Any]]:
        return [self.read(n, window_s, now_sim_t) for n in self.series]
