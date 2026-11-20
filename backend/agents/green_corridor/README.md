# GreenCorridor: Emergency Vehicle Priority (Traffora AI)

GreenCorridor is a standalone module that detects an ambulance in a SUMO simulation, plans its
route to a hospital, monitors the signalised intersections ahead of it, and issues safe signal
priority requests so it does not wait at every junction. It measures the result against a
normal-signals run of the same scenario.

Scope of this phase: GreenCorridor only. There is no orchestrator, no agent-to-agent
communication and no shared backend here. Those belong to the later team phase.

## Components

| File | Responsibility |
|---|---|
| `connection.py` | Start/stop SUMO, TraCI connection handling, typed connection errors |
| `emergency_detector.py` | Identify ambulances, track state, speed, road, destination, status |
| `route_planner.py` | Route calculation on the real SUMO network (NetworkX), blocked-route checks, recalculation |
| `intersection_monitor.py` | Upcoming/current intersections, signal phase, ETA, crossing log |
| `priority_manager.py` | Signal priority requests, safety validation, restore after the ambulance passes |
| `journey_monitor.py` | Journey metrics and the baseline-vs-priority comparison (CLI) |
| `scenario_builder.py` | Generates the sample city network and the baseline/priority scenarios |
| `visualizer.py` | Standalone dashboard built from measured results |
| `schemas.py` | JSON-serialisable data types and the `ok()` / `fail()` result envelope |
| `config.py`, `api_utils.py` | Settings; error guard used by the public functions |

A separate `agent.py` was not needed: the public functions below are the module's interface.

## Setup (Windows 11 + WSL, as used in development)

```
python -m venv ~/gc-venv
source ~/gc-venv/bin/activate
pip install -r requirements-greencorridor.txt
python simulation/green_corridor/verify_setup.py
```

`verify_setup.py` checks SUMO, TraCI and the other dependencies. If you installed SUMO with the
Windows MSI instead, run the same commands from a Windows terminal in the project's virtual
environment.

## Run it

Build the sample scenario (only needed if `simulation/green_corridor/scenarios` is empty):

```
python -m backend.agents.green_corridor.scenario_builder
```

Run both simulations and print the comparison table. This writes
`simulation/green_corridor/results/comparison.json`:

```
python -m backend.agents.green_corridor.journey_monitor
python -m backend.agents.green_corridor.journey_monitor --no-respect-min-dur
```

The first command is the conservative default. The second lets the priority manager shorten a
conflicting green below SUMO's minimum phase duration. Both still go through the program's own
yellow and all-red intervals. The results file holds whichever command ran last, so run the
default last if you want the dashboard to show the default mode.

Draw the dashboard (saved to `simulation/green_corridor/results/dashboard.png`):

```
python -m backend.agents.green_corridor.visualizer
python -m backend.agents.green_corridor.visualizer --interactive
```

The visualiser reads `comparison.json` and the network file. It does not start SUMO.

## Public functions

Every public function returns the same JSON-serialisable envelope and never raises:

```
{"success": true,  "data": {...}, "error": null, "timestamp": "..."}
{"success": false, "data": null,  "error": {"code": "...", "message": "..."}, "timestamp": "..."}
```

| Function | Module | Purpose |
|---|---|---|
| `detect_emergency_vehicle(detector)` | `emergency_detector` | Detect ambulances currently in the simulation |
| `get_emergency_status(detector, vehicle_id)` | `emergency_detector` | State of one ambulance: position, road, speed, status, distance travelled |
| `calculate_emergency_route(planner, destination_edge, vehicle_id=None, ...)` | `route_planner` | Plan the route; returns edges, distance, estimated time, intersections |
| `check_emergency_route(planner, vehicle_id)` | `route_planner` | Check the active route against current conditions |
| `recalculate_emergency_route(planner, vehicle_id, apply=True)` | `route_planner` | Re-plan when the route is blocked or a faster one exists |
| `identify_upcoming_intersections(monitor, vehicle_id, ...)` | `intersection_monitor` | Upcoming intersections with distance, ETA and signal state; crossed intersections |
| `generate_signal_priority_request(manager, vehicle_id)` | `priority_manager` | Run one priority step; returns the requests and actions |
| `restore_normal_operation(manager, tls_id)` | `priority_manager` | Return one signal to its normal program |
| `monitor_emergency_journey(journey, vehicle_id)` | `journey_monitor` | Live journey metrics |
| `compare_baseline_vs_priority(scenario, vehicle_id=None, ...)` | `journey_monitor` | Run both scenarios and return the comparison |

Check exact parameters with `help(function_name)` or by reading the function signatures.

## Safe signal priority

The priority manager follows these rules. Please confirm each against `priority_manager.py` before
presenting it:

- It never sets phases directly. It extends the ambulance's own green, or shortens a conflicting
  green to a floor of 4.0 s and lets the signal program advance through its own yellow and
  all-red into the ambulance's phase.
- It acts only on the ambulance's next intersections, using the estimated arrival time.
- After the ambulance crosses, it restores the normal phase duration and normal operation.
- Failures are reported in `priority_errors`, not hidden.

## Measured results

From `simulation/green_corridor/results/comparison.json` (vehicle `amb_1`, seed 42, one run per
mode):

| Metric | Normal signals | Priority |
|---|---|---|
| Journey time | 234 s | 180 s |
| Average speed | 36.6 km/h | 47.5 km/h |
| Waiting at intersections | 45 s | 0 s |
| Stopped time | 45 s | 0 s |
| Intersections crossed | 7 | 7 |
| Distance travelled | about 2377 m | about 2376 m |

Time saved: 54 s (23.1%). Priority requests: 7, all restored.

These numbers come from the simulation. They are one scenario with one seed, not an average, so
they should not be quoted as a general improvement figure.

## Tests

```
python -m pytest backend/agents/green_corridor/tests -q
```

128 tests passed at the end of Step 8. The visualiser tests added in Step 10 do not need SUMO.
Update this line with the final count after your last full run.

## Limitations

- One network and one scenario; results are not averaged over many seeds or traffic levels.
- The dashboard's ambulance track is interpolated between measured junction times, and it shows
  priority actions as a timeline, not as live signal colours.
- The module works on a SUMO simulation only; there is no connection to real signal hardware.

## Notes for team integration (later phase)

Nothing here calls another agent. The functions above and the `ok()` / `fail()` envelope are the
proposed interface, so the orchestrator can call them and decide whether priority requests are
approved centrally. Decisions needed then: who owns signal control when TrafficMind is also
changing phases, how priority requests are approved, and how ParkSense shares road state.