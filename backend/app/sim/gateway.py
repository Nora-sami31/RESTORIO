"""Telemetry gateway + isolated decoy endpoint.

Specification sections 03 and 06.  The gateway is the only thing standing
between a client and operational state, so it owns three jobs:

  * forward PT-101's local value to the controller (and let a permitted test
    client overwrite that forwarded field -- scenario B),
  * enforce containment once a client's operational write access is revoked,
  * route a contained client's later requests to a decoy with entirely
    separate state.

The access log it writes is a *trusted* component in the demo's threat model.
Section 06 states that assumption plainly; so does the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .records import EvidenceItem, new_id, now

OPERATIONAL = "operational"
DECOY = "decoy"


@dataclass
class AccessEvent:
    event_id: str
    timestamp: float
    client_id: str
    action: str                 # read | write | write_blocked
    target: str
    field_name: str | None
    old_value: Any = None
    new_value: Any = None
    authorized: bool = True
    route: str = OPERATIONAL
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "client_id": self.client_id,
            "action": self.action,
            "target": self.target,
            "field": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "authorized": self.authorized,
            "route": self.route,
            "note": self.note,
        }


@dataclass
class DecoyState:
    """Completely separate state. Never read as operational feedback.

    The decoy has to *behave* like the thing it imitates, or a client probing
    it learns immediately that its writes do nothing. Each accepted write
    therefore moves decoy state the way the same write would have moved
    operational state -- while touching nothing real.
    """
    base_bar: float = 4.02
    forwarded_offset_bar: float = 0.0
    pt101_forwarded_bar: float = 4.02
    setpoint_bar: float = 4.0
    pump_output_pct: float = 50.0
    writes_received: int = 0
    interactions: list[dict[str, Any]] = field(default_factory=list)

    def apply_write(self, field_name: str, value: Any) -> tuple[Any, Any]:
        """Mirror the operational effect of a write. Returns (old, new)."""
        if field_name == "pt101_forwarded_offset_bar":
            old = self.forwarded_offset_bar
            self.forwarded_offset_bar = float(value)
            self.pt101_forwarded_bar = round(self.base_bar + float(value), 3)
            return old, self.forwarded_offset_bar
        if field_name == "pt101_forwarded_bar":
            old = self.pt101_forwarded_bar
            self.pt101_forwarded_bar = float(value)
            self.base_bar = float(value) - self.forwarded_offset_bar
            return old, self.pt101_forwarded_bar
        if field_name in ("setpoint_bar", "pump_output_pct"):
            old = getattr(self, field_name)
            setattr(self, field_name, float(value))
            return old, getattr(self, field_name)
        return None, None


class Gateway:
    def __init__(self) -> None:
        # Clients permitted to write operational telemetry fields.
        self.write_acl: set[str] = {"hmi-operator-01", "test-client-07"}
        self.known_clients: set[str] = {"hmi-operator-01", "test-client-07", "plc-101"}
        self.routes: dict[str, str] = {}      # client_id -> OPERATIONAL | DECOY
        self.contained: set[str] = set()
        self.access_log: list[AccessEvent] = []

        # The forwarded telemetry field. None means "pass the local value
        # straight through", which is the healthy behaviour.
        self.forwarded_override_bar: float | None = None
        self.forwarded_offset_bar: float = 0.0
        self.override_author: str | None = None
        self.offset_author: str | None = None

        self.decoy = DecoyState()
        self.decoy_active = False

        # Forwarding-path health. When the path degrades the gateway keeps
        # handing the controller the last value it managed to deliver, while
        # retries accumulate behind it. The value is not WRONG, it is STALE --
        # which looks identical to tampering from the controller's seat.
        self.fault_stale_forwarding = False
        self.forward_frozen_value: float | None = None
        self.comms_retry_total = 0
        self.comms_retries_per_min = 0.0
        self.last_forward_sim_t = 0.0
        self.forward_latency_ms = 12.0

        self._local_value_fn = None
        self._clock = lambda: 0.0

    def bind_local_source(self, fn) -> None:
        self._local_value_fn = fn

    def bind_clock(self, fn) -> None:
        self._clock = fn

    def tick(self, sim_t: float, dt: float) -> None:
        """Advance forwarding-path health. Called once per simulation step."""
        if self.fault_stale_forwarding:
            if self.forward_frozen_value is None:
                self.forward_frozen_value = self.forwarded_value()
                self.last_forward_sim_t = sim_t
            self.comms_retry_total += 1
            self.comms_retries_per_min = min(
                60.0, self.comms_retries_per_min + dt * 9.0)
            self.forward_latency_ms = (sim_t - self.last_forward_sim_t) * 1000.0
        else:
            self.forward_frozen_value = None
            self.last_forward_sim_t = sim_t
            self.comms_retries_per_min = max(
                0.0, self.comms_retries_per_min - dt * 14.0)
            self.forward_latency_ms = 12.0

    def restart_forwarding(self) -> dict[str, Any]:
        """Clear the retry backlog and resume live forwarding."""
        was = self.fault_stale_forwarding
        held = self.forward_frozen_value
        self.fault_stale_forwarding = False
        self.forward_frozen_value = None
        self.comms_retries_per_min = 0.0
        self.forward_latency_ms = 12.0
        return {"ok": True, "was_degraded": was, "released_held_value": held,
                "retries_cleared": self.comms_retry_total}

    # -- the forwarded value the controller consumes ---------------------
    def forwarded_value(self) -> float:
        if self.fault_stale_forwarding and self.forward_frozen_value is not None:
            return self.forward_frozen_value      # held: the path is not refreshing
        local = self._local_value_fn() if self._local_value_fn else 0.0
        if self.forwarded_override_bar is not None:
            return self.forwarded_override_bar
        return local + self.forwarded_offset_bar

    def route_for(self, client_id: str) -> str:
        return self.routes.get(client_id, OPERATIONAL)

    # -- client interaction ----------------------------------------------
    def client_write(self, client_id: str, field_name: str, value: Any,
                     note: str = "") -> dict[str, Any]:
        """A client attempts to change a forwarded telemetry field.

        Three outcomes, each logged:
          * routed to decoy  -> decoy state changes, plant untouched
          * not authorized   -> blocked, plant untouched
          * authorized       -> forwarded field changes (this is scenario B)
        """
        route = self.route_for(client_id)

        if route == DECOY:
            old, new = self.decoy.apply_write(field_name, value)
            accepted = new is not None
            self.decoy.writes_received += 1
            self.decoy.interactions.append({
                "timestamp": now(), "client_id": client_id,
                "field": field_name, "attempted_value": value,
                "decoy_response": "accepted" if accepted else "unknown_field",
                "previous": old, "decoy_value_now": new,
                "decoy_forwarded_bar": round(self.decoy.pt101_forwarded_bar, 3),
            })
            ev = self._log(client_id, "write", "decoy-endpoint", field_name,
                           old, value, authorized=True, route=DECOY,
                           note="Routed to isolated decoy; operational state unaffected.")
            return {"accepted": True, "route": DECOY, "event_id": ev.event_id,
                    "operational_effect": "none"}

        if client_id not in self.write_acl:
            ev = self._log(client_id, "write_blocked", "telemetry-gateway", field_name,
                           None, value, authorized=False, route=OPERATIONAL,
                           note=note or "Client has no operational write authorization.")
            return {"accepted": False, "route": OPERATIONAL, "event_id": ev.event_id,
                    "operational_effect": "none", "reason": "not_authorized"}

        if field_name == "pt101_forwarded_offset_bar":
            old = self.forwarded_offset_bar
            self.forwarded_offset_bar = float(value)
            self.offset_author = client_id
        else:
            old = self.forwarded_override_bar
            if field_name == "pt101_forwarded_bar":
                self.forwarded_override_bar = float(value)
                self.override_author = client_id
        ev = self._log(client_id, "write", "telemetry-gateway", field_name,
                       old, value, authorized=True, route=OPERATIONAL, note=note)
        return {"accepted": True, "route": OPERATIONAL, "event_id": ev.event_id,
                "operational_effect": "forwarded_telemetry_changed"}

    # -- containment -------------------------------------------------------
    def contain_client(self, client_id: str, revoke_write: bool = True,
                       activate_decoy: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {"client_id": client_id}
        if revoke_write:
            self.write_acl.discard(client_id)
            result["write_access"] = "revoked"
        self.contained.add(client_id)

        # Revoking access stops FURTHER writes. It does not undo the value the
        # client already latched into the forwarded field -- that stays until an
        # engineer refreshes it, which is why recovering control needs the
        # predefined trusted feedback path (specification section 06).
        if self.override_author == client_id or self.offset_author == client_id:
            result["latched_modification"] = "still_present"
            result["forwarded_field_still_modified"] = True

        if activate_decoy:
            self.routes[client_id] = DECOY
            self.decoy_active = True
            result["route"] = DECOY

        self._log(client_id, "contain", "telemetry-gateway", None, None, None,
                  authorized=True, route=self.route_for(client_id),
                  note="Operational write access revoked by validated response.")
        result["ok"] = True
        return result

    def containment_proof(self, client_id: str) -> dict[str, Any]:
        """Actively test that a contained client cannot reach operational state.

        Section 10 requires this to be measured, not asserted.
        """
        before_override = self.forwarded_override_bar
        before_offset = self.forwarded_offset_bar
        before_decoy = self.decoy.pt101_forwarded_bar
        probe = self.client_write(client_id, "pt101_forwarded_bar", 1.234,
                                  note="Containment verification probe.")
        probe2 = self.client_write(client_id, "pt101_forwarded_offset_bar", 2.5,
                                   note="Containment verification probe (offset field).")
        after_override = self.forwarded_override_bar
        reached_operational = (after_override != before_override
                               or self.forwarded_offset_bar != before_offset)
        return {
            "client_id": client_id,
            "probe_route": probe.get("route"),
            "probe_accepted": probe.get("accepted"),
            "probe_offset_accepted": probe2.get("accepted"),
            "reached_operational_state": reached_operational,
            "operational_override_before": before_override,
            "operational_override_after": after_override,
            "decoy_value_before": before_decoy,
            "decoy_value_after": self.decoy.pt101_forwarded_bar,
            "isolation_holds": not reached_operational,
        }

    # -- logging -----------------------------------------------------------
    def _log(self, client_id: str, action: str, target: str, field_name: str | None,
             old: Any, new: Any, authorized: bool, route: str, note: str) -> AccessEvent:
        ev = AccessEvent(
            event_id=new_id("ACC"), timestamp=now(), client_id=client_id,
            action=action, target=target, field_name=field_name,
            old_value=old, new_value=new, authorized=authorized,
            route=route, note=note,
        )
        self.access_log.append(ev)
        return ev

    def access_evidence(self, since: float | None = None) -> list[EvidenceItem]:
        items = []
        for ev in self.access_log:
            if since is not None and ev.timestamp < since:
                continue
            items.append(EvidenceItem(
                evidence_id=ev.event_id,
                source_id="trusted-access-log",
                kind="access_event",
                timestamp=ev.timestamp,
                asset_ids=["telemetry-gateway", "PT-101"],
                title=f"{ev.action} by {ev.client_id} on {ev.field_name or ev.target}",
                excerpt=(
                    f"client={ev.client_id} action={ev.action} target={ev.target} "
                    f"field={ev.field_name} old={ev.old_value} new={ev.new_value} "
                    f"authorized={ev.authorized} route={ev.route}. {ev.note}"
                ),
                provenance="Gateway trusted access log (assumed uncompromised, see limits).",
                payload=ev.to_dict(),
            ))
        return items

    def status(self) -> dict[str, Any]:
        return {
            "forwarded_override_bar": self.forwarded_override_bar,
            "forwarded_offset_bar": round(self.forwarded_offset_bar, 3),
            "override_author": self.override_author,
            "offset_author": self.offset_author,
            "write_acl": sorted(self.write_acl),
            "contained_clients": sorted(self.contained),
            "routes": dict(self.routes),
            "decoy_active": self.decoy_active,
            "forwarding_degraded": self.fault_stale_forwarding,
            "comms_retries_per_min": round(self.comms_retries_per_min, 1),
            "forward_latency_ms": round(self.forward_latency_ms, 1),
            "decoy": {
                "pt101_forwarded_bar": round(self.decoy.pt101_forwarded_bar, 3),
                "forwarded_offset_bar": round(self.decoy.forwarded_offset_bar, 3),
                "setpoint_bar": self.decoy.setpoint_bar,
                "pump_output_pct": self.decoy.pump_output_pct,
                "writes_received": self.decoy.writes_received,
                "interactions": self.decoy.interactions[-20:],
            },
            "access_events": len(self.access_log),
        }

    def clone(self) -> "Gateway":
        """Deep copy for sandbox testing. Shares no state with the original."""
        g = Gateway()
        g.write_acl = set(self.write_acl)
        g.known_clients = set(self.known_clients)
        g.routes = dict(self.routes)
        g.contained = set(self.contained)
        g.access_log = []                       # sandbox writes are not audit events
        g.forwarded_override_bar = self.forwarded_override_bar
        g.forwarded_offset_bar = self.forwarded_offset_bar
        g.override_author = self.override_author
        g.offset_author = self.offset_author
        g.decoy = DecoyState(
            base_bar=self.decoy.base_bar,
            forwarded_offset_bar=self.decoy.forwarded_offset_bar,
            pt101_forwarded_bar=self.decoy.pt101_forwarded_bar,
            setpoint_bar=self.decoy.setpoint_bar,
            pump_output_pct=self.decoy.pump_output_pct,
            writes_received=self.decoy.writes_received,
            interactions=list(self.decoy.interactions),
        )
        g.decoy_active = self.decoy_active
        g.fault_stale_forwarding = self.fault_stale_forwarding
        g.forward_frozen_value = self.forward_frozen_value
        g.comms_retry_total = self.comms_retry_total
        g.comms_retries_per_min = self.comms_retries_per_min
        g.last_forward_sim_t = self.last_forward_sim_t
        g.forward_latency_ms = self.forward_latency_ms
        return g
