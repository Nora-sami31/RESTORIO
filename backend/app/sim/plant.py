"""Simulated pumping station: TK-101, P-101, PT-101, PT-102, PLC-101.

Specification section 03.  Two rules govern this file:

  1. Ground truth lives in `Plant.truth` and is NEVER returned by any method
     the agent can reach.  `agent_*` methods are the only permitted surface.
  2. The control loop is deterministic and runs independently of model
     latency.  If the LLM stalls or the API fails, the plant keeps running
     and keeps protecting itself.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from .records import (
    QUALITY_BAD,
    QUALITY_GOOD,
    QUALITY_STALE,
    QUALITY_UNCERTAIN,
    Measurement,
    now,
)

# --------------------------------------------------------------------------
# Device facts.  These are properties of the physical transmitter and are the
# same no matter what the configuration says -- that gap is the whole point of
# scenario A.
# --------------------------------------------------------------------------
PT101_INSTALLED_RANGE_MIN = 0.0
PT101_INSTALLED_RANGE_MAX = 20.0      # the device actually fitted in the field
PT101_SERIAL = "SN-4471-A"
PT101_MODEL = "Kallisto PTX-5200"

MA_MIN, MA_SPAN = 4.0, 16.0           # 4-20 mA current loop


def pressure_to_ma(pressure_bar: float,
                   range_min: float = PT101_INSTALLED_RANGE_MIN,
                   range_max: float = PT101_INSTALLED_RANGE_MAX) -> float:
    """Physical transmitter output. Uses the INSTALLED range, always."""
    span = max(range_max - range_min, 1e-9)
    frac = (pressure_bar - range_min) / span
    frac = min(max(frac, 0.0), 1.0)
    return MA_MIN + MA_SPAN * frac


def ma_to_pressure(ma: float, range_min: float, range_max: float) -> float:
    """Receiving-system decode. Uses whatever range the CONFIG says.

    When config range != installed range you get a plausible but wrong number,
    which is exactly the scenario A failure.
    """
    span = max(range_max - range_min, 1e-9)
    frac = (ma - MA_MIN) / MA_SPAN
    return range_min + frac * span


# --------------------------------------------------------------------------
# Configuration -- versioned, visible to the agent, changed only by executor.
# --------------------------------------------------------------------------
@dataclass
class StationConfig:
    pt101_range_min: float = 0.0
    pt101_range_max: float = 20.0
    pt101_unit: str = "bar"
    feedback_source: str = "PT-101"       # PT-101 | PT-102
    setpoint_bar: float = 4.0
    kp: float = 14.0
    ki: float = 7.0
    controller_mode: str = "auto"         # auto | safe_state | manual
    manual_output_pct: float = 50.0       # the held output while in manual

    def snapshot(self) -> dict[str, Any]:
        return {
            "pt101_range_min": self.pt101_range_min,
            "pt101_range_max": self.pt101_range_max,
            "pt101_unit": self.pt101_unit,
            "feedback_source": self.feedback_source,
            "setpoint_bar": self.setpoint_bar,
            "kp": self.kp,
            "ki": self.ki,
            "controller_mode": self.controller_mode,
            "manual_output_pct": self.manual_output_pct,
        }


@dataclass
class GroundTruth:
    """Hidden from the agent. Used for evaluation and the audience overlay."""
    pressure_bar: float = 4.0
    tank_level_pct: float = 72.0
    pump_output_pct: float = 50.0
    pump_rpm: float = 1450.0
    demand: float = 1.0
    tripped: bool = False
    trip_reason: str = ""
    active_faults: list[str] = field(default_factory=list)
    # Mechanical condition of the pump, 1.0 = as-new. Degradation is a
    # PHYSICAL fault: every measurement stays correct and in agreement, the
    # machine simply cannot deliver. No configuration change can repair it.
    pump_efficiency: float = 1.0
    # A downstream restriction holds pressure up regardless of pump output.
    # Dead-heading: the controller backs off to zero and pressure stays high.
    restriction_floor_bar: float = 0.0


class Plant:
    """First-order pressure model with a PI controller.

    dP/dt = (A*u - B*demand - C*P) / tau

    Chosen so that u=50% at nominal demand settles at exactly 4.00 bar, and
    full output reaches 8.5 bar -- enough headroom for the scenario-A
    overpressure to be real rather than cosmetic.
    """

    A = 9.0
    B = 0.5
    C = 1.0
    TAU = 2.0

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed if seed is not None else settings.seed)
        self.config = StationConfig()
        self.config_revision = 7          # the station has a history
        self.truth = GroundTruth()
        self.sim_time = 0.0
        self._integral = 0.0
        self._last_feedback: float | None = None

        # Fault switches. Hidden; set only by the scenario loader.
        self.fault_scaling_active = False
        self.fault_pt102_degraded = False
        self.fault_pt102_missing = False
        # Transmitter drift: the physical sensor itself reads high or low.
        # Not a configuration fault and not tampering.
        self.fault_pt101_drift_bar = 0.0

        # The range of the transmitter PHYSICALLY fitted. Hidden ground truth.
        self.installed_range_max = PT101_INSTALLED_RANGE_MAX
        # The range the asset registry BELIEVES is fitted -- this is what
        # get_asset reports. The two diverge whenever paperwork lags reality,
        # which is the most ordinary failure in a real plant.
        self.registry_range_max = PT101_INSTALLED_RANGE_MAX
        self.registry_model = PT101_MODEL
        self.registry_serial = PT101_SERIAL
        # A calibration error on the *reference* instrument. When this is set,
        # the independent measurement is the thing that is wrong.
        self.pt102_offset_bar = 0.0
        # Reference available and accurate, but NOT certified as a control
        # source -- a real distinction in instrumented plants. You may compare
        # against it; you may not hand the controller to it.
        self.fault_pt102_uncertain = False

        # PT-102 freshness bookkeeping.
        self._pt102_last_update = 0.0        # simulation clock, not wall clock
        self._pt102_cached: float | None = None

    # -- physics ---------------------------------------------------------
    def step(self, dt: float) -> None:
        t = self.truth

        if t.tripped:
            # Latched protective state: pump commanded to minimum, pressure
            # bleeds down. Nothing the agent says can override this.
            t.pump_output_pct = 0.0
        else:
            t.pump_output_pct = self._controller_output(dt)

        u = t.pump_output_pct / 100.0
        target = self.A * t.pump_efficiency * u - self.B * t.demand
        dp = (target - self.C * t.pressure_bar) / self.TAU
        t.pressure_bar += dp * dt
        # A restriction downstream sets a floor the pump cannot relieve.
        t.pressure_bar = max(t.restriction_floor_bar, t.pressure_bar, 0.0)

        t.pump_rpm = 400.0 + 26.0 * t.pump_output_pct
        # Tank slowly draws down with flow, refills when pump is idle.
        t.tank_level_pct += dt * (0.35 - 0.012 * t.pump_output_pct)
        t.tank_level_pct = min(max(t.tank_level_pct, 5.0), 100.0)

        # Independent protection -- not part of the control loop.
        if t.pressure_bar >= settings.overpressure_trip_bar and not t.tripped:
            t.tripped = True
            t.trip_reason = (
                f"Overpressure trip: true line pressure reached "
                f"{t.pressure_bar:.2f} bar (limit {settings.overpressure_trip_bar:.1f})."
            )
            self.config.controller_mode = "safe_state"

        self.sim_time += dt

    def _controller_output(self, dt: float) -> float:
        """PI controller, PLC-101. Consumes whichever feedback is configured."""
        if self.config.controller_mode == "safe_state":
            self._integral = 0.0
            return 0.0

        if self.config.controller_mode == "manual":
            # Left in manual after maintenance: the output is held and the
            # feedback is ignored entirely. Every measurement stays correct
            # and in agreement -- the loop simply is not closed.
            self._integral = 0.0
            return min(max(self.config.manual_output_pct, 0.0), 100.0)

        fb = self.controller_feedback_value()
        if fb is None:
            # No usable feedback -- hold last output rather than slamming it.
            return self.truth.pump_output_pct

        err = self.config.setpoint_bar - fb
        self._integral += err * dt
        # Anti-windup clamp.
        self._integral = min(max(self._integral, -40.0), 40.0)
        u = self.config.kp * err + self.config.ki * self._integral
        self._last_feedback = fb
        return min(max(u, 0.0), 100.0)

    # -- sensor chain ----------------------------------------------------
    def pt101_raw_ma(self) -> float:
        """Physical 4-20 mA output of the installed transmitter.

        `fault_pt101_drift_bar` models the transmitter itself reading off: the
        signal leaving the device is wrong, so no configuration change and no
        containment can repair it.
        """
        noise = self.rng.gauss(0.0, settings.noise_bar * 0.4)
        return pressure_to_ma(
            self.truth.pressure_bar + noise + self.fault_pt101_drift_bar,
            PT101_INSTALLED_RANGE_MIN, self.installed_range_max)

    def pt101_local_value(self) -> float:
        """Raw signal decoded with the CURRENT configuration.

        Healthy: equals true pressure.  Scenario A: equals half of it.
        """
        return ma_to_pressure(
            self.pt101_raw_ma(),
            self.config.pt101_range_min,
            self.config.pt101_range_max,
        )

    def pt102_value(self) -> tuple[float | None, str, float]:
        """Independent reference measurement. Returns (value, quality, age_s).

        Ageing is measured on the simulation clock so that a fast replay and a
        live demo produce identical results.
        """
        tnow = self.sim_time
        if self.fault_pt102_missing:
            return None, QUALITY_BAD, tnow - self._pt102_last_update

        if self.fault_pt102_degraded:
            # Updates only intermittently -> goes stale, must not be trusted
            # blindly as a failover target.
            if tnow - self._pt102_last_update > 9.0:
                self._pt102_last_update = tnow
                self._pt102_cached = (self.truth.pressure_bar + self.pt102_offset_bar
                                  + self.rng.gauss(0.0, settings.noise_bar * 6))
            age = tnow - self._pt102_last_update
            quality = QUALITY_STALE if age > settings.staleness_limit_s else QUALITY_UNCERTAIN
            return self._pt102_cached, quality, age

        self._pt102_last_update = tnow
        self._pt102_cached = (self.truth.pressure_bar + self.pt102_offset_bar
                              + self.rng.gauss(0.0, settings.noise_bar))
        quality = QUALITY_UNCERTAIN if self.fault_pt102_uncertain else QUALITY_GOOD
        return self._pt102_cached, quality, 0.0

    def controller_feedback_value(self) -> float | None:
        """What PLC-101 actually consumes right now."""
        if self.config.feedback_source == "PT-102":
            val, quality, _age = self.pt102_value()
            if val is None or quality in (QUALITY_BAD, QUALITY_STALE):
                return None
            return val
        # PT-101 path goes through the gateway; the engine injects the
        # forwarded value via set_forwarded_hook.
        return self._forwarded_hook() if self._forwarded_hook else self.pt101_local_value()

    _forwarded_hook = None

    def set_forwarded_hook(self, fn) -> None:
        """Engine wires the gateway's forwarded value in here."""
        self._forwarded_hook = fn

    # -- measurement objects the agent may read --------------------------
    def measurement_pt101_local(self) -> Measurement:
        ma = self.pt101_raw_ma()
        return Measurement(
            asset_id="PT-101",
            source_id="local-trusted-read",
            timestamp=now(),
            value=round(ma_to_pressure(ma, self.config.pt101_range_min,
                                       self.config.pt101_range_max), 3),
            unit=self.config.pt101_unit,
            quality=QUALITY_GOOD,
            config_revision=self.config_revision,
            raw_signal_ma=round(ma, 3),
            age_s=0.0,
        )

    def measurement_pt102(self) -> Measurement:
        val, quality, age = self.pt102_value()
        return Measurement(
            asset_id="PT-102",
            source_id="reference-trusted-path",
            timestamp=now(),
            value=None if val is None else round(val, 3),
            unit="bar",
            quality=quality,
            config_revision=self.config_revision,
            raw_signal_ma=None if val is None else round(pressure_to_ma(val), 3),
            age_s=round(age, 2),
        )

    # -- executor-only mutations -----------------------------------------
    def bump_revision(self) -> int:
        self.config_revision += 1
        return self.config_revision

    def reset_integral(self) -> None:
        self._integral = 0.0

    def clear_trip(self) -> bool:
        if not self.truth.tripped:
            return False
        self.truth.tripped = False
        self.truth.trip_reason = ""
        self.config.controller_mode = "auto"
        self._integral = 0.0
        return True

    # -- views -------------------------------------------------------------
    def truth_overlay(self) -> dict[str, Any]:
        """Audience-only. Explicitly excluded from every agent tool."""
        t = self.truth
        return {
            "true_pressure_bar": round(t.pressure_bar, 3),
            "tank_level_pct": round(t.tank_level_pct, 1),
            "pump_output_pct": round(t.pump_output_pct, 1),
            "pump_rpm": round(t.pump_rpm, 0),
            "demand": round(t.demand, 2),
            "pump_efficiency": round(t.pump_efficiency, 3),
            "restriction_floor_bar": round(t.restriction_floor_bar, 2),
            "tripped": t.tripped,
            "trip_reason": t.trip_reason,
            "active_faults": list(t.active_faults),
        }

    # -- cloning, for isolated candidate testing -------------------------
    def clone(self) -> "Plant":
        """Deep copy for sandbox testing. Shares no state with the original."""
        c = Plant.__new__(Plant)
        c.rng = random.Random()
        c.rng.setstate(self.rng.getstate())
        c.config = StationConfig(**self.config.snapshot())
        c.config_revision = self.config_revision
        c.truth = GroundTruth(
            pump_efficiency=self.truth.pump_efficiency,
            restriction_floor_bar=self.truth.restriction_floor_bar,
            pressure_bar=self.truth.pressure_bar,
            tank_level_pct=self.truth.tank_level_pct,
            pump_output_pct=self.truth.pump_output_pct,
            pump_rpm=self.truth.pump_rpm,
            demand=self.truth.demand,
            tripped=self.truth.tripped,
            trip_reason=self.truth.trip_reason,
            active_faults=list(self.truth.active_faults),
        )
        c.sim_time = self.sim_time
        c._integral = self._integral
        c._last_feedback = self._last_feedback
        c.fault_scaling_active = self.fault_scaling_active
        c.fault_pt102_degraded = self.fault_pt102_degraded
        c.fault_pt102_missing = self.fault_pt102_missing
        c.fault_pt101_drift_bar = self.fault_pt101_drift_bar
        c.installed_range_max = self.installed_range_max
        c.registry_range_max = self.registry_range_max
        c.registry_model = self.registry_model
        c.registry_serial = self.registry_serial
        c.pt102_offset_bar = self.pt102_offset_bar
        c.fault_pt102_uncertain = self.fault_pt102_uncertain
        c._pt102_last_update = self._pt102_last_update
        c._pt102_cached = self._pt102_cached
        c._forwarded_hook = None
        return c
