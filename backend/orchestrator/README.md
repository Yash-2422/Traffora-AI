# Traffora-AI — Central Orchestrator

The orchestrator is the composition root of Traffora-AI. It runs **one** SUMO
session, lets the three existing agents observe that session, resolves their
recommendations by a fixed priority order, and is the only component allowed to
write to a traffic light — and only after an independent safety check.

Nothing in this package rewrites the agents. `backend/agents/traffic_mind`,
`backend/agents/green_corridor` and `backend/agents/park_sense` are used
exactly as they ship; the orchestrator wraps them in adapters.

---

## 1. Architecture

```
                        ┌──────────────────────────────┐
                        │      TrafforaOrchestrator    │
                        │  (composition root, run_step)│
                        └───────────────┬──────────────┘
                                        │
        ┌───────────────┬───────────────┼───────────────┬────────────────┐
        │               │               │               │                │
┌───────▼──────┐ ┌──────▼───────┐ ┌─────▼──────┐ ┌──────▼──────┐ ┌───────▼────────┐
│ AgentRegistry│ │ EventManager │ │  Decision  │ │   Safety    │ │  Simulation    │
│  (adapters)  │ │ (priority +  │ │   Engine   │ │  Validator  │ │  Coordinator   │
│              │ │  dedupe)     │ │ (P1>P2>P3) │ │ (gate)      │ │ (one TraCI)    │
└───────┬──────┘ └──────┬───────┘ └─────┬──────┘ └──────┬──────┘ └───────┬────────┘
        │               │               │               │                │
        └───────────────┴───────┬───────┴───────────────┴────────────────┘
                                │
                        ┌───────▼────────┐
                        │  SharedState   │  (thread-safe, RLock)
                        │  CityState /   │
                        │  metrics /     │
                        │  decisions     │
                        └────────────────┘
```

### One control cycle (`run_step`)

1. `coordinator.step(n)` — advance the shared SUMO session.
2. `_collect(now)` — sample every available agent (throttled by
   `decision_interval_s`). A failing agent degrades; it never aborts the cycle.
3. `process_events(snapshots)` — publish agent events, fold payloads into shared
   state, then drain the queue in priority order.
4. `state.snapshot()` — build an immutable `CityState`.
5. `decisions.decide(...)` — deterministic, priority-ordered decisions.
6. `_validate_and_apply(...)` — safety-validate every signal action; apply only
   `VALID` ones.
7. Record decisions, measured metrics and cycle latency.

Steps 4–6 are pure Python over dictionaries, so they are unit-testable with no
SUMO installed.

### Module map

| File | Responsibility |
| --- | --- |
| `exceptions.py` | Typed error hierarchy with `to_dict()` for API responses |
| `config.py` | `OrchestratorConfig`, `SafetyLimits`, `DecisionSettings`, logging setup |
| `schemas.py` | All Pydantic models and enums (single source of truth) |
| `shared_state.py` | Thread-safe latest city state, agent health, decisions, metrics |
| `agent_registry.py` | `AgentAdapter` base + the three adapters + registry |
| `event_manager.py` | Priority queue with validation, dedupe and history |
| `decision_engine.py` | Deterministic P1/P2/P3 resolution |
| `safety_validator.py` | Independent gate for every signal write |
| `simulation_coordinator.py` | The only module that imports TraCI |
| `orchestrator.py` | Composition root + FastAPI-ready read methods |
| `scripts/run_orchestrator.py` | CLI runner, writes a measured JSON report |
| `scripts/run_demo.py` | Narrated demonstration timeline |

---

## 2. Agent integration

The agents return three different shapes, so adapters normalise them:

* **TrafficMind** — Pydantic models (`model_dump()`).
* **GreenCorridor / ParkSense** — dataclasses with `to_dict()`.
* Some helpers already return plain dicts.

`agent_registry._as_dict()` handles all three, so the orchestrator only ever
works on dictionaries. Adapters call the agents' **existing** public entry
points; no agent file is modified.

| Adapter | Wraps | Produces |
| --- | --- | --- |
| `TrafficMindAdapter` | TrafficMind monitoring/analytics | `TrafficState`, congestion events, green-extension recommendations |
| `GreenCorridorAdapter` | GreenCorridor detection + route monitoring | `EmergencyEvent`, `SIGNAL_PRIORITY_REQUEST` events, preemption actions |
| `ParkSenseAdapter` | ParkSense occupancy + demand | `ParkingState`, occupancy/demand events, ranked zone advice |

Every adapter inherits `AgentAdapter.collect()`, which wraps `_collect()` in a
`try/except`: an exception marks the agent `DEGRADED` and returns an empty
snapshot instead of breaking the cycle.

Agents stay independently testable — their own suites run unchanged (see §6).

---

## 3. Schemas

All models are Pydantic v2, JSON-serialisable (`model_dump(mode="json")`), and
use `ConfigDict(extra="ignore")` so an agent adding a field cannot break the
orchestrator.

| Model | Purpose |
| --- | --- |
| `CityState` | One instant of the city: traffic, emergencies, parking, agents, latest decision |
| `TrafficState` | Vehicle count, waiting time, queue length, congestion level, per-junction levels |
| `EmergencyEvent` | One emergency vehicle: id, tls, distance, ETA, route |
| `ParkingState` / `ParkingZoneState` | Per-zone and aggregate occupancy |
| `AgentStatus` | `agent`, `health`, `available`, `message`, `latency_ms` |
| `AgentRecommendation` | Normalised agent output: signal actions, parking advice, explanation |
| `SignalAction` | A proposed write: `tls_id`, `duration_s`, `phase_index`, provenance |
| `ParkingRecommendation` | Advisory zone ranking |
| `OrchestratorDecision` | Final decision + `validation` + `applied` + `rejected_reason` |
| `ValidationResult` | `VALID` / `REJECTED` / `DEFERRED` with reasons |
| `SystemMetrics` | Measured metrics only |
| `SystemStatus` | Running flag, config, agents, counters, metrics |
| `Event` | id, type, source agent, timestamp, priority, payload, status, `dedupe_key` |

Enums: `EventPriority` (EMERGENCY=1, TRAFFIC=2, PARKING=3), `EventType`,
`EventStatus`, `AgentName`, `AgentHealth`, `DecisionType`, `ActionType`,
`ValidationStatus`.

---

## 4. Event priorities and conflict resolution

`EventManager` orders by `(priority, timestamp, sequence)`, so an emergency
event always drains before a traffic event of the same tick, and ties are FIFO.

Duplicate suppression uses `Event.stable_key()`: an explicit `dedupe_key` when
the agent supplies one, otherwise `type|agent|junction_id|tls_id|vehicle_id|
zone_id`. The window is `event_dedupe_window_s` (default 30 s).

`DecisionEngine` resolves conflicts with a fixed order:

1. **P1 — Emergency.** A GreenCorridor preemption always wins. Any TrafficMind
   action for the same light is dropped for that step. Two emergency vehicles
   targeting one light produce **one** write (the longer hold wins,
   deterministically). A light is written at most once per
   `signal_action_cooldown_s`, so a preemption cannot pin a green forever.
2. **P2 — Traffic.** Only applied where no emergency owns the light, only when
   the junction level is in `traffic_action_levels` (default `HIGH`), and
   rate-limited per light by `traffic_cooldown_s` to avoid unnecessary
   switching.
3. **P3 — Parking.** Advisory only. `_parking_decisions()` never emits a signal
   action, so it cannot interfere with P1 or P2.

There is no LLM and no randomness in this path. Same inputs → same decisions.

---

## 5. Safety validation

`SafetyValidator` is independent of the decision engine: the coordinator refuses
to write anything that does not carry a `VALID` result, and refuses the **whole
batch** if one action is invalid (no half-updated junctions).

Rules enforced:

* Only `SET_PHASE_DURATION` / `RESTORE_SIGNAL` are permitted.
* The traffic light must exist in the running network (`require_known_tls`).
* `duration_s` must be positive and finite.
* No live state → `DEFERRED` (never guessed).
* **Clearance intervals (yellow / all-red) are untouchable.**
* **The phase index may never change.** A different index could introduce a
  conflicting green, so it is rejected. Because the program's own conflict-free
  phase sequence stays in force, two conflicting simultaneous greens are
  structurally impossible.
* Minimum green: `max(min_green_s, program minDur)`; emergency preemption uses
  `min_emergency_green_s` instead (preemption is the one legitimate reason to
  cut a green short).
* Maximum green measured over the **whole phase**.
* `min_remaining_green_s`, `max_extension_s`, `max_cycle_s`.
* Emergency provenance: an emergency action must name a vehicle, and a
  non-emergency action may not claim one.

### The `setPhaseDuration` subtlety

`setPhaseDuration` sets the **remaining** duration of the running phase, and
SUMO ends the phase once that many seconds have elapsed *in* it. The green
actually served by a write is therefore `max(elapsed, requested)` — not
`elapsed + requested`, and not `requested` alone. The validator uses that
figure for both the minimum and maximum green checks; using the wrong one let a
sequence of short holds pin a phase green indefinitely (caught by a regression
test).

### Signal program

`simulation/orchestrator/signals.add.xml` (generated by
`scripts/build_signal_programs.py`) re-declares the network's own link mapping
under `programID="orchestrator"`, with explicit all-red clearance after every
yellow and declared `minDur`/`maxDur` (green 15–60 s, yellow 3 s, all-red 2 s).
The coordinator activates it once at startup with `trafficlight.setProgram`.
SUMO refuses two `tlLogic` entries with the same id + programID, which is why a
distinct program ID is required.

---

## 6. Setup and execution

### Requirements

Python 3.10+, `sumo` on `PATH`, and the orchestrator requirements:

```powershell
pip install -r requirements-orchestrator.txt
```

> There is no root `requirements.txt` in this repository — each component ships
> its own file (`requirements-greencorridor.txt`,
> `backend/agents/*/requirements.txt`, and `requirements-orchestrator.txt` for
> this package). `requirements-orchestrator.txt` is the union needed to run the
> orchestrator, because it imports all three agents.
>
> The sklearn pin matters: the shipped TrafficMind models were pickled with
> 1.9.1 and raise on unpickle under other versions. With 1.7.2 the full
> TrafficMind suite passes (253 tests), so the pin is in the requirements file.

### Run the orchestrator (Windows PowerShell)

```powershell
# default scenario, 1800 s
python -m backend.orchestrator.scripts.run_orchestrator

# 900 s, 2x demand, write a JSON report
python -m backend.orchestrator.scripts.run_orchestrator --end 900 --scale 2 --json-out runs/full.json

# with the SUMO GUI
python -m backend.orchestrator.scripts.run_orchestrator --gui

# fixed-time baseline: agents still observe, nothing is written to SUMO
python -m backend.orchestrator.scripts.run_orchestrator --no-signal-control --json-out runs/fixed.json

# TrafficMind only
python -m backend.orchestrator.scripts.run_orchestrator --no-emergency --no-parking --json-out runs/traffic.json
```

### Demonstration

```powershell
python -m backend.orchestrator.scripts.run_demo --end 900 --scale 2 --json-out runs/demo.json
```

### Three-way comparison

```powershell
bash scripts/run_comparison.sh
```

(On Windows, run the three `run_orchestrator` commands above; the shell script
is just a wrapper.)

### HTTP API

`backend/orchestrator/api.py` exposes the service methods over FastAPI. It is a
transport layer only: it never decides, never validates and never writes to
SUMO. Importing the module does **not** start SUMO — the orchestrator is built
lazily on the first request that needs it.

```powershell
# serve on http://127.0.0.1:8000, interactive docs at /docs
python -m backend.orchestrator.api --port 8000

# or with uvicorn directly
uvicorn backend.orchestrator.api:app --port 8000
```

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness probe; never builds the orchestrator |
| `GET` | `/status` | Config, agent health, pending events, metrics |
| `POST` | `/simulation/start` | Initialise SUMO + agents, mark running |
| `POST` | `/simulation/stop` | Close the shared SUMO session |
| `POST` | `/simulation/step?n=1` | Advance `n` steps; returns the new decisions |
| `GET` | `/city-state` | Latest traffic / emergency / parking / agent snapshot |
| `GET` | `/agents` | One health entry per agent |
| `GET` | `/decisions/latest` | Most recent decision, or `null` before cycle 1 |
| `GET` | `/decisions?limit=50` | Decision history, newest first |
| `GET` | `/events?limit=100` | Recent events, oldest first |
| `GET` | `/emergency` | Active emergency vehicles plus emergency events |
| `GET` | `/analytics/traffic` | Waiting time, queue length, travel time |
| `GET` | `/analytics/parking` | Occupancy and search time |
| `GET` | `/metrics` | Every measured metric, nothing estimated |

The simulation is advanced **explicitly** via `POST /simulation/step`, so there
is no background thread racing the caller and the HTTP surface stays
deterministic. Calling `/simulation/step` while stopped returns `409` with a
stable error code:

```json
{"error": {"code": "SIMULATION_NOT_RUNNING", "message": "the simulation is not running; call POST /simulation/start first"}}
```

A typical dashboard loop:

```powershell
curl -X POST http://127.0.0.1:8000/simulation/start
curl -X POST "http://127.0.0.1:8000/simulation/step?n=30"
curl http://127.0.0.1:8000/city-state
curl http://127.0.0.1:8000/analytics/traffic
curl -X POST http://127.0.0.1:8000/simulation/stop
```

### Deployment

The API is a normal long-lived process, so it needs a container host rather
than a serverless platform. The repository ships `Dockerfile`, `.dockerignore`
and `render.yaml` for Render; Railway and Fly.io accept the same Dockerfile
unchanged. `requirements-orchestrator-linux.txt` adds the `eclipse-sumo` wheel,
which supplies the `sumo` and `netconvert` binaries on Linux, and the entry
point honours `$PORT` and binds `0.0.0.0` when it is injected.

Vercel cannot host this service: it is static/serverless only, and the
orchestrator owns a long-lived SUMO subprocess, a TraCI socket and in-memory
state across requests. See [`DEPLOYMENT.md`](../../DEPLOYMENT.md) at the
repository root for the full reasoning and the step-by-step Render procedure.

---

## 7. Testing

```powershell
# whole orchestrator suite
python -m pytest backend/orchestrator/tests -q

# individual modules
python -m pytest backend/orchestrator/tests/test_safety_validator.py -q
python -m pytest backend/orchestrator/tests/test_decision_engine.py -q
python -m pytest backend/orchestrator/tests/test_integration_mocked.py -q

# real-SUMO end-to-end (skips automatically if SUMO is absent)
python -m pytest backend/orchestrator/tests/test_simulation_e2e.py -q
```

### Actual results (measured, not estimated)

| Suite | Result |
| --- | --- |
| `test_shared_state.py` | 11 passed |
| `test_event_manager.py` | 13 passed |
| `test_safety_validator.py` | 16 passed |
| `test_agent_registry.py` | 10 passed |
| `test_decision_engine.py` | 13 passed |
| `test_orchestrator.py` | 16 passed |
| `test_simulation_coordinator.py` | 15 passed |
| `test_integration_mocked.py` | 4 passed |
| `test_simulation_e2e.py` (real SUMO) | 7 passed |
| `test_api.py` (FastAPI, fakes) | 17 passed |
| `test_api_e2e.py` (FastAPI + real SUMO) | 8 passed |
| **Orchestrator total** | **131 passed** |
| TrafficMind (regression) | 253 passed |
| GreenCorridor (regression) | 138 passed |
| ParkSense (regression) | 110 passed |

The real-SUMO test discovers 21 traffic lights and 5 parking areas in
`unified.sumocfg`, asserts that SUMO actually advanced and vehicles departed,
and asserts that every applied signal decision carries a `VALID` validation
record.

`test_api_e2e.py` repeats that check through the HTTP surface: it starts a real
simulation over the API, steps it, and verifies the JSON carries measured
figures and that the coordinator's write count still equals the number of
accepted signal actions — so the API cannot become a way to bypass the safety
validator.

---

## 8. Example output

`python -m backend.orchestrator.scripts.run_demo --end 900 --scale 2`
(seed 42, demand ×2) produced this timeline from live agent output:

```
-- Agents initialised against the shared SUMO session ----------------------
   green_corridor READY      monitoring emergency vehicles
   park_sense     READY      5 parking zones loaded
   traffic_mind   READY      attached to shared simulation

-- Control loop ------------------------------------------------------------
   t=     0s  PARKING      Parking optimisation (P3, advisory): recommend PA_D
                            (15 free spaces, 0.0% occupied). This decision
                            cannot change a traffic signal.
   t=   145s  EMERGENCY    Emergency priority (P1): GreenCorridor requested
                            preemption at A2. Active vehicles: amb_1 -> A2
                            (ETA 17.9s); amb_1.1 -> A2 (ETA 19.6s) TrafficMind
                            actions for these lights are suppressed this step.
                safety: VALID (all safety rules passed)
                applied to SUMO: True
   t=   295s  CONGESTION   Traffic optimisation (P2): 1 green extension(s) at
                            D2. Only lights with HIGH congestion and no active
                            emergency request were adjusted.

-- Measured results --------------------------------------------------------
   simulated time            : 900s
   vehicles departed/arrived : 616 / 597
   average waiting time      : 3.329s
   average queue length      : 0.538 veh/edge
   average travel time       : 149.235s
   emergency travel time     : 206.5s
   emergency response time   : 22.5s
   parking occupancy         : 27.45%
   accepted / rejected       : 191 / 0
   processing latency        : 42.039ms
   agent availability        : 100.0%

-- Safety ------------------------------------------------------------------
   signal decisions applied  : 3
   signal decisions rejected : 0
   applied without VALID     : 0  (must be 0)
```

### Comparison (900 s, scale 2, seed 42 — identical demand and seed)

| Metric | Fixed-time | TrafficMind only | Full Traffora-AI |
| --- | --- | --- | --- |
| Vehicles departed / arrived | 616 / 591 | 616 / 595 | 616 / 597 |
| Avg waiting time (s) | 3.523 | 3.424 | 3.329 |
| Avg queue length (veh/edge) | 0.559 | 0.543 | 0.538 |
| Avg travel time (s) | 148.020 | 147.974 | 149.235 |
| Emergency travel time (s) | 262.5 | 262.5 | **206.5** |
| Emergency response time (s) | — | — | 22.5 |
| Parking occupancy (%) | 27.45 | — | 27.45 |
| Accepted / rejected decisions | 180 / 16 | 3 / 0 | 191 / 0 |
| Orchestrator latency (ms) | 40.836 | 19.093 | 41.540 |
| Agent availability (%) | 100.0 | 100.0 | 100.0 |

**How to read this honestly.** Under this demand the network is not saturated,
so the traffic-signal effect is small: waiting time improves by ~0.19 s
(3.523 → 3.329, about 5 %) and queue length by ~0.02 veh/edge, which is within
run-to-run noise. The clear, reproducible effect is on the emergency vehicle:
travel time drops from 262.5 s to 206.5 s (−56 s, ~21 %) because GreenCorridor's
preemption is actually applied. Average travel time is *slightly worse* in the
full configuration (149.235 vs 148.020 s) — preemption delays cross traffic, and
that trade-off is intentional. No percentage here is extrapolated beyond the
measured runs.

The fixed-time row reports 16 rejected decisions because signal control is
disabled in baseline mode; those rejections are the baseline switch itself, not
safety failures.

---

## 9. Known limitations

* **Demand is light.** The 5×5 grid at scale 2 does not saturate, so traffic
  optimisation has little headroom. A higher `--scale` (3+) is needed to show a
  large signal-control benefit, and at that level the network can gridlock.
* **Parking search time is not measured.** ParkSense does not expose it, so
  `parking_search_time_s` is `null` rather than estimated.
* **The HTTP layer is a thin transport only.** `api.py` mounts the service
  methods on FastAPI and does no control work: it never decides, never
  validates and never writes to SUMO. The simulation is advanced explicitly
  through `POST /simulation/step`, so nothing runs on a background thread.
* **Parking is advisory only.** No routing of vehicles to the recommended zone
  is implemented; the recommendation is surfaced for the dashboard.
* **Single scenario.** Everything runs on the GreenCorridor 5×5 network. The
  original TrafficMind and ParkSense networks are not merged into it.
* **Emergency vehicles can duplicate.** SUMO may report `amb_1` and `amb_1.1`
  for the same physical vehicle; both are tracked separately.

### Future work

* Stream `CityState` over WebSocket instead of polling `/city-state`.
* Feed parking recommendations into vehicle routing.
* Add a saturated-demand scenario for a meaningful signal-control benchmark.
* Measure parking search time from actual cruising distance.

---

## 10. Git

All work is on the `integration` branch only. Agent files are untouched; the
orchestrator is additive (`backend/orchestrator/`, `simulation/orchestrator/`,
`scripts/`).
