# Architecture

## Separation of concerns

The whole safety argument rests on one boundary: **the model proposes, a
deterministic validator tests, and a separate executor is the only thing that
writes.**

```
          ┌────────────────────────────────────────────────┐
          │  Plant (physics)   PLC-101 (PI control loop)   │   ground truth
 sim/     │  Gateway (forwarding, containment, routing)    │   lives here and
          │  Decoy (entirely separate state)               │   is never exposed
          └───────────────┬────────────────────────────────┘
                          │  agent-visible reads only
          ┌───────────────▼────────────────────────────────┐
 agent/   │  tools  →  loop  →  incident lifecycle         │
          │  brain: anthropic | openai | ollama | offline  │
          └───────────────┬────────────────────────────────┘
                          │  Proposal
          ┌───────────────▼────────────────────────────────┐
 control  │  Validator    permitted action? fields in      │
          │               range? bound to current revision?│
          │               claim supported by the reference?│
          │               safe and non-regressive in a     │
          │               sandboxed copy?                  │
          │  Executor     the ONLY writer. Bumps revision. │
          └───────────────┬────────────────────────────────┘
                          │  events
          ┌───────────────▼────────────────────────────────┐
 web      │  SSE → React + Three.js operator website       │
          │  SQLite → persistent, inspectable audit trail  │
          └────────────────────────────────────────────────┘
```

## The process model

`backend/app/sim/plant.py`

```
dP/dt = (A·u − B·demand − C·P) / τ        A=9.0  B=0.5  C=1.0  τ=2.0 s
```

Tuned so 50 % pump output at nominal demand settles at exactly 4.00 bar, with
headroom to 8.5 bar — enough that the scenario-A overpressure is real rather
than cosmetic. An independent protective trip latches a safe state at 9.0 bar
regardless of what the controller or the agent want.

### The sensor chain, which is the whole puzzle

```
true pressure ──► transmitter ──► raw mA ──► PLC decode ──► gateway ──► PLC-101
                  (INSTALLED            (CONFIGURED        (may be
                   0-20 bar)             range)             modified)
```

Three distinct numbers, and their relationships identify the fault:

| | forwarded | local read | reference | reads as |
|---|---|---|---|---|
| healthy | 4.00 | 4.00 | 4.00 | all agree |
| **A scaling** | 4.00 | 4.00 | 7.98 | local == forwarded, both ≠ reference |
| **B tampering** | 4.00 | 5.50 | 5.48 | local == reference, forwarded differs |
| **C both** | 4.00 | 3.50 | 6.98 | all three differ |

The discriminator the agent must find: decode the raw mA with the **installed**
device range. If that reproduces the independent reference, the configuration is
wrong. If the local read already matches the reference and only the forwarded
value differs, the discrepancy was introduced after the transmitter.

## Agent tools

`backend/app/agent/tools.py`

| Tool | Purpose | Constraint |
|---|---|---|
| `read_telemetry` | forwarded, local + raw mA, reference with quality and age | excludes simulator truth |
| `get_asset` | equipment metadata | PT-101 returns installed device *and* configured scaling |
| `compare_configs` | diff two revisions, or a revision against live | — |
| `search_evidence` | work orders, specs, config snapshots, notes, access log | returns IDs that must be cited |
| `check_scaling` | 4-20 mA → engineering units | deterministic arithmetic, not judgement |
| `record_hypotheses` | competing explanations with stances | **required** before proposing |
| `test_response` | validate + run on an isolated copy | nothing operational changes |
| `apply_validated_response` | apply | refused without a passing, revision-bound report |
| `verify_recovery` | independent checks on live samples | the only thing that can confirm recovery |
| `conclude` | end the incident | invariants may downgrade the claim |

## Incident lifecycle

```
detected → investigating → response_proposed → validating → applying → verifying
               ▲                                                            │
               │                                                            ▼
               │                                                       monitoring
               │                                                            │
               └──────── the repair did not hold ───────────────────────────┤
                                                                            ▼
recovered | recovered_under_containment | needs_review | safe_state_unresolved
```

Verification and monitoring are separate stages on purpose. Verification asks
whether the process is within limits *now*; monitoring asks whether it still is
after a further window. A repair that passes the first and fails the second
returns the incident to `investigating` rather than closing it, because the
likely explanation is a second, different fault.

A failed check cannot transition to recovered. `Incident.conclude` enforces three
invariants regardless of what the model says:

1. `recovered` without a passing verification → downgraded to `needs_review`.
2. Confidence below the threshold → cannot claim a repaired state.
3. A client was contained → reclassified as `recovered_under_containment`.
4. A `conclude` whose monitoring window fails is rejected outright; the run
   continues instead of ending, so the agent cannot close a station that is
   still drifting.

## Validation, in order

1. Is the action in the catalogue?
2. Is the proposal bound to the **current** configuration revision?
3. Are all changed fields whitelisted for that action and inside their ranges?
4. Action-specific prerequisites — fallback freshness, access-log support.
5. Is the claim supported? A proposed scaling must reproduce the independent
   reference from the live raw signal.
6. Sandbox: clone the plant and gateway, apply, run forward, and confirm the
   response is safe and does not make the process worse.

Step 6 deliberately does **not** require full recovery. When two faults are
present, no single bounded action can recover the station, and demanding it
would deadlock the agent into doing nothing. Live verification decides whether
the incident is actually over.

## Records

Every record carries the identifiers needed to click an asset and land on the
exact event: `Measurement`, `EvidenceItem`, `Hypothesis`, `Proposal`,
`TestReport`, `Execution`, `VerificationResult`. Defined in
`backend/app/sim/records.py`, persisted to SQLite in `backend/app/store/audit.py`.

## Model providers

`backend/app/agent/brain.py`. All HTTP uses the standard library — a demo should
not be able to fail because a vendor SDK changed. `offline` is a deterministic
reasoner driving the same tools through the same loop; it is both the no-key
fallback and the rules-based evaluation baseline.

## Determinism

The detector, sensor ageing and freshness all run on the **simulation clock**,
not wall clock, so a fast replay and a live demo produce identical results. Seeds
are explicit. `RESTORIO_SPEED` scales wall-clock pacing without changing outcomes.
