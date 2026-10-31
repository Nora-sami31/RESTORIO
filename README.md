# RESTORIO

**Agentic investigation, containment, and verified recovery for industrial systems.**

> The dashboard reads **4.00 bar**. The pipe is at **8.00**. Nothing is alarming,
> because nothing is broken — the controller is doing its job perfectly, on a
> measurement that is wrong.

*Is the equipment failing, is its configuration wrong, or can we no longer trust its data?*

RESTORIO connects operational diagnosis and cybersecurity containment in one
evidence-driven recovery workflow. Its LLM agent investigates anomalies, compares
competing explanations, proposes targeted responses, and checks whether recovery
actually holds.

The working prototype demonstrates this workflow through a dynamic industrial
pumping-station simulation, with a 3D operator dashboard and a persistent audit trail.

Built for **Challenge 1 — Agentic Recovery of a Failing System**.

---

## The problem

An industrial system looks healthy on every screen while the equipment behind it
runs past its limits. Incorrect configuration, manipulated telemetry, stale
measurements and equipment degradation produce **indistinguishable** initial
symptoms while requiring different responses.

In RESTORIO's configuration-fault scenario, a backup restore reinstates an outdated
sensor range. The controller underestimates pressure and drives the pump harder:
**the dashboard reaches 4 bar while actual pressure reaches 8 bar**. The failure
follows a correctly executed maintenance procedure — and restoring the most recent
backup, the standard first response to almost any control problem, is what
reinstates it.

A different scenario introduces an OT cyberattack through a client that alters
forwarded telemetry. Repairing the sensor configuration does not stop that
manipulation, just as containing the client does not correct an unrelated
configuration error.

When both faults occur together, a partial repair is insufficient. Recovery requires
identifying the remaining cause and verifying the resulting process behaviour.

---

## What RESTORIO does

1. **Detects anomalies** in measurement agreement, signal freshness, and process performance.
2. **Investigates evidence** across telemetry, raw signals, logs, metrics, configurations, equipment specifications, and maintenance records.
3. **Compares explanations** before selecting a response.
4. **Proposes targeted repairs or containment**, or escalates when software intervention is unsupported.
5. **Validates each intervention** through deterministic checks and testing on an isolated simulation copy.
6. **Applies validated responses**, with human approval according to the selected autonomy mode.
7. **Verifies recovery using live process samples** and investigates again when a repair is insufficient.
8. **Monitors stability before closure**, then rearms detection after successful recovery.

The operator dashboard and audit trail expose the evidence, hypotheses, proposals,
tests, actions, verification, and monitoring results.

A second agent reads the accumulated trail and forecasts what is likely to fail
next. See [Forecasting the next failure](#forecasting-the-next-failure).

---

## Why it is agentic

In LLM mode, the model selects evidence queries and response tools based on the
investigation so far. It receives operational observations rather than the scenario
label or injected-fault list, and unit tests assert that the true cause is excluded
from every surface it can read.

Its decisions depend on evidence:

- A configuration mismatch justifies a scaling correction.
- An evidenced telemetry modification justifies client containment and a trusted feedback path.
- A forwarding-service fault requires a different response from deliberate manipulation.
- Mechanical degradation requires maintenance rather than a software change.

Failed verification supplies new evidence. The agent revises its diagnosis and
investigates another cause instead of treating the first applied action as success.

A deterministic offline reasoner also runs through the same tools and controls. It
provides a no-key demonstration mode and a repeatable rules baseline.

---

## Operational recovery and cyber containment

RESTORIO demonstrates both operational and cyber-related failures:

| Scenario | Recovery behavior |
|---|---|
| Incorrect sensor scaling | Check the raw signal against equipment specifications and test a configuration correction |
| Manipulated telemetry | Identify the modifying client from operational evidence, contain it, and select a trusted feedback path |
| Configuration error and manipulation together | Address the faults through multiple actions, using verification to identify incomplete recovery |
| Stale forwarding | Investigate gateway logs and metrics and test a forwarding-service response |
| Controller left in manual | Investigate the control state and test restoration of automatic control |
| Pump degradation or downstream blockage | Escalate with an evidence-based maintenance recommendation |
| Missing or unreliable reference evidence | Refuse unsupported recovery claims and escalate or enter a protective state |
| Healthy station | Avoid unnecessary intervention |

Containment revokes the responsible client's operational access and redirects
subsequent writes to an isolated decoy. The decoy has separate state and supports an
inspectable isolation test.

Maintenance escalation is recorded for operator follow-up; the prototype does not
create tickets in an external maintenance system.

---

## Independent controls around model decisions

**The model proposes; a deterministic validator checks; a separate executor applies.**

A proposed intervention must satisfy enforced controls, including:

- A permitted action and whitelisted fields.
- Valid parameter ranges.
- A test bound to the current configuration revision.
- Action-specific prerequisites.
- Supporting operational evidence.
- An isolated candidate test for safety and non-regression within the simulation.

Containment requires an operational write in the trusted access log. Sensor
disagreement alone does not justify it.

Switching control to a reference requires that reference to meet the implemented
usability checks.

The candidate test does not require a single action to achieve complete recovery.
This allows bounded progress when multiple faults coexist; live verification
determines whether the overall incident is resolved.

The process setpoint is deliberately **not writable**. Moving the target to match a
broken reading would redefine success instead of recovering anything.

### Verification and monitoring

A recovery narrative is insufficient to close an incident.

RESTORIO checks live process samples against recovery criteria, then requires a
further monitoring window. Failed monitoring rejects the attempted recovery
conclusion and allows investigation to continue.

Investigations are bounded by iteration, tool-call, application, and time budgets.
Unresolved cases escalate rather than retry indefinitely.

Successful recovery rearms detection after a simulation-time grace period.
Unresolved incidents hold detection with a visible reason to avoid repeatedly
opening the same incident.

### Human oversight

Three autonomy modes are available:

- **Supervised:** approval for every intervention.
- **Guarded:** approval for high-risk interventions.
- **Full:** validated interventions execute without an approval pause.

---

## Forecasting the next failure

The audit trail is also a dataset. A separate agent reads it and answers a
different question from the investigation loop: not what is wrong now, but what
is likely to go wrong next. It appears at the top of **Incident history**.

It is built the way the investigation loop is built. Read-only tools supply
facts -- recurrence statistics over closed incidents, the assets each fault
class was actually recorded against, and a least-squares fit over the live
control signals. The model chooses which to read, decides which fault class
matters, and writes the claim. A deterministic claim check then tests that
conclusion against the record before an operator sees it.

The claim check rejects a forecast that:

- cites a fault class absent from the record,
- rests on **any** class with fewer than three recorded occurrences,
- names an asset that appears in no recorded diagnosis, or that cannot be
  checked because the cited classes name no assets at all,
- states **when** something will happen without a cited class that is actually
  recurring on an interval,
- projects past the end of the recorded history,
- or carries a confidence outside (0, 1].

A rejected forecast is returned to the model with the failing checks attached,
and it revises rather than being discarded.

These checks test **structure, not wording**. They establish that the forecast
rests on classes that exist and are thick enough, that its asset is in the
record, that its horizon fits, and that any timing claim is supported. They do
not verify that the model's prose quotes the record correctly, so the dashboard
renders the recorded figures for every cited class beside the model's own
wording rather than asking an operator to take it on trust.

### What it refuses to say

The refusals matter more than the forecast:

- A fault class seen once has **no interval**. The record reports "single
  occurrence" rather than inventing a period from one data point.
- A class is called periodic only when it has at least two intervals and those
  intervals are consistent. Occurrences whose gaps scatter as widely as their
  mean, or whose last gap dwarfs that mean, are reported as **not periodic**
  instead of raising an "overdue" alarm the data does not support.
- A live window that does not fit a line is reported as **not projectable**
  rather than extrapolated.

The leading indicator for pressure loss is not pressure. A working controller
holds pressure while it still has authority, so the projection tracks commanded
pump output climbing toward saturation, past which discharge pressure falls.

### Its limits

The forecast is advisory. It is **not admissible as evidence for an
intervention**, and the forecasting agent has no write tools -- it cannot reach
the validator or the executor. A unit test asserts that its tool surface stays
read-only.

The forecast horizon is bounded by the record itself: the horizon check refuses
any projection longer than the span the database currently holds. A short record
therefore yields forecasts in hours rather than days, and cannot support a claim
about next week until the history grows to cover one.

Those incidents were raised by **running scenarios against the simulator**. The
gaps between them record when a scenario was triggered, not how often equipment
fails, and the dashboard, the prompt and the stored record all say so. Outcome
counts per fault class -- how often a class escalated rather than recovering --
do not depend on that timing and remain meaningful. Establishing real failure
frequencies would need operational or reliability data this prototype does not
have.

Offline, a rules baseline composes the same statistics into the same structure
and is reported as rules rather than as reasoning.

---

## Evaluation and evidence

Evaluation covers task outcomes, adaptation, regression checks, and the contribution
of independent guardrails.

### Acceptance and held-out testing

| Reasoner | Acceptance set | Held-out set |
|---|---:|---:|
| Deterministic offline reasoner | **10/10** | **15/15** |
| Claude LLM — developer-reported runs | **10/10** | **15/15** |

The offline results were reproduced during the final package review. The Claude
figures are developer-reported: `./scripts/test.sh` runs the deterministic reasoner
so that it needs no API key, and does not re-execute them.

The held-out suite contains five scenarios across three seeds, including reversed
fault directions, a different operating setpoint, compound faults, and transmitter
drift. These cases also serve as repeatable regression tests.

Passing means reaching an acceptable outcome without applying forbidden actions.
Some cases correctly end in escalation rather than recovery.

The packaged SQLite database contains selected LLM investigation records. These
provide inspectable behavioral evidence; they are not a complete per-case export of
the reported LLM suites.

### Adaptive behavior in saved LLM investigations

The saved traces include:

- **Compound-fault recovery:** an initial repair is insufficient, verification fails, and the investigation continues.
- **Transmitter-drift recovery:** the LLM uses the existing trusted-feedback action to restore operation without making an unsupported scaling correction or cyber classification.
- **Mechanical-fault escalation:** the agent records a maintenance recommendation without applying an unsupported software intervention.
- **Selected generated-case recoveries:** the LLM investigates seeded fault compositions through the existing tool catalogue.

These traces demonstrate adaptive tool use within the simulated environment.

### Deterministic baseline comparison

`docs/evaluation_results.json` contains a saved comparison across six scenarios and
three seeds:

| Metric | Restore latest backup | RESTORIO deterministic reasoner |
|---|---:|---:|
| Correct outcomes | 3/18 | **18/18** |
| Verified recoveries | 0 | **9** |
| Incorrect interventions | 0 | **0** |
| False cyber classifications | 0 | **0** |

This is a comparison of deterministic strategies, not an LLM benchmark.

The restore-latest strategy performs poorly because the backup itself reintroduces
the configuration fault in the scaling scenario.

### Generated incidents

The generator composes incidents from defined fault primitives, varying conditions,
fault combinations, and available evidence. Seeds allow incidents to be replayed.

Recoverable cases are checked using candidate responses on an isolated copy; other
cases deliberately expect escalation.

The saved generated evaluation records:

- **27/40 correct outcomes** for the rules baseline.
- **15/24 correct outcomes** among cases where recovery was expected.

Selected generated LLM successes are available in the audit database. A complete
paired LLM evaluation over the same 40 cases is not included, so these results do
not establish an aggregate LLM advantage.

### Guardrail ablation

The saved ablation evaluates individual controls and runs a deliberately fallible
reasoner that makes unsupported proposals or recovery claims.

| Configuration | Unsupported actions | False recoveries |
|---|---:|---:|
| All guardrails active | **0** | **0** |
| Evidence gate removed | 5 | 0 |
| Claim check removed | 2 | 0 |
| Conclusion invariants removed | 0 | 10 |

These are observed results from the specified simulation battery, not a guarantee
against every possible failure.

Removing individual guardrails did not change the successful deterministic
reasoner's outcomes in that battery. The fallible-reasoner results demonstrate why
independent enforcement matters when reasoning is wrong.

Evaluation also informed implementation improvements, including preventing an agent
from redefining success by changing the process setpoint.

---

## Run locally

The launcher installs missing backend dependencies and builds the frontend if its
build is absent, so a fresh clone needs no preparation.

### Offline demonstration

No API key is required:

```bash
RESTORIO_PROVIDER=offline ./scripts/start.sh
```

Open **http://127.0.0.1:8080**.

### LLM demonstration

Copy the configuration template:

```bash
cp .env.example .env
```

Set the desired provider and its credentials in `.env`.

Supported providers include Anthropic, Kimi/Moonshot, OpenAI-compatible endpoints,
and local Ollama.

Check the configured provider:

```bash
python3 scripts/check_provider.py
```

Start the application:

```bash
./scripts/start.sh
```

Keep `.env` local. Submit `.env.example` with empty credentials.

The launcher stops a process occupying its configured port before starting the demo
server.

---

## Run the checks

Run the deterministic unit, acceptance, and held-out suites:

```bash
RESTORIO_PROVIDER=offline ./scripts/test.sh
```

Run the other evaluations separately:

```bash
python3 scripts/evaluate.py
python3 scripts/ablate.py
python3 scripts/generated.py --n 40
```

Run an individual scenario. `RESTORIO_AUTONOMY=full` is required here: the
default `guarded` mode waits for an approval that a headless run cannot give.

```bash
RESTORIO_PROVIDER=offline RESTORIO_AUTONOMY=full python3 scripts/run_scenario.py compound
```

The saved JSON files describe recorded evaluation runs. Fresh results should be
interpreted alongside the selected scenarios, seeds, reasoner, configuration, and
code revision.

---

## Inspect the saved investigations

`backend/restorio_audit.db` ships with recorded LLM investigations from earlier
runs, and every further run appends to it. The dashboard, the recurrence
statistics and the forecast all read whatever the database currently holds, so
no figure in the system is tied to a fixed incident count.

Open **Incident history** in the dashboard to inspect their evidence, explanations,
interventions, and recovery results.

These records demonstrate successful recovery, recovery under containment, and
escalation. They are recorded runs, not newly executed investigations on the
reviewer's machine.

---

## Implementation

| Component | Responsibility |
|---|---|
| `backend/app/sim/` | Dynamic plant, controller, sensors, gateway, decoy, scenarios, validation, and execution |
| `backend/app/agent/` | Twelve tools, model providers, investigation loop, incident lifecycle, offline reasoner, and the forecasting agent |
| `backend/app/store/` | Persistent SQLite incident audit and recurrence analysis |
| `frontend/` | React and Three.js operator dashboard |
| `scripts/` | Startup, provider checks, scenario runs, regression tests, and evaluation |
| `docs/` | Architecture and saved evaluation results |

See [Architecture](docs/ARCHITECTURE.md) for the implementation structure.

---

## Scope and next steps

RESTORIO is a functional recovery prototype demonstrated in a dynamic industrial
simulation. The environment models pressure, control behaviour, sensor
interpretation, telemetry forwarding, operational evidence, and response effects.

The results establish behaviour within this environment. Production downtime
savings, industrial safety certification, and performance on deployed plant
equipment have not been measured.

Next steps include a paired generated-case LLM benchmark, broader tests of
unreliable evidence and reference paths, and controlled integration with external
industrial telemetry.

**RESTORIO's contribution is the complete, inspectable path from uncertain evidence
to justified intervention and verified recovery.**
