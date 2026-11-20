# TrafficMind integration checklist

For the Phase 2 Central Orchestrator. TrafficMind is finished as a standalone agent. It does not start SUMO, step the simulation, change a signal, or talk to GreenCorridor or ParkSense.

Public entry point: `backend/agents/traffic_mind/agent.py`.

Input and output shapes: [INPUT_OUTPUT.md](INPUT_OUTPUT.md).

## What to call

| Function | When | Returns |
|---|---|---|
| `attach_simulation(sim)` | once, after the simulation is running | nothing (raises `TrafficMindError` if `sim` is not running) |
| `get_traffic_status()` | every sample | traffic-status dict |
| `detect_congestion()` | every sample, after the status call | congestion dict |
| `predict_congestion()` | every sample, after congestion | forecast dict (`horizon_s` 30, 60 or 120; default 60) |
| `generate_signal_recommendation()` | every sample, after the forecast | signal-advice dict (`mode` is always `"recommend_only"`) |
| `reset_agent()` | when the simulation is closed or restarted | nothing |

`load_signal_programs({junction_id: program})` replaces `attach_simulation` when the data is a recording rather than a live simulation. The program shape is `RawSignalProgram` in the input/output reference.

Each of the four sample functions returns a plain dict and does not raise.

- Success: `"status": "ok"` and the fields in the input/output reference.
- Failure: `{"status": "error", "code": "...", "message": "..."}`. Codes are listed at the end of the input/output reference.

```python
from backend.agents.traffic_mind.agent import (
    attach_simulation,
    detect_congestion,
    generate_signal_recommendation,
    get_traffic_status,
    predict_congestion,
    reset_agent,
)
from backend.agents.traffic_mind.config import SimulationConfig
from backend.agents.traffic_mind.simulation_interface import SimulationInterface

with SimulationInterface(SimulationConfig(scenario="mixed")) as sim:
    attach_simulation(sim)
    while sim.current_time < 1800:
        sim.step(5)                                      # the orchestrator steps SUMO
        status = get_traffic_status()
        congestion = detect_congestion()
        forecast = predict_congestion()                 # or predict_congestion(horizon_s=30)
        advice = generate_signal_recommendation()
        if advice["status"] != "ok":
            continue                                     # code + message describe the failure
        # decide here whether to apply advice["recommendations"]
reset_agent()
```

Without a simulation, pass the previous function's dict (or a recorded traffic-status dict) as `snapshot`:

```python
status = get_traffic_status(recorded_status)
congestion = detect_congestion()       # uses the status just supplied
```

## Checklist

- [ ] Import TrafficMind only. Do not instantiate its monitor, detector, predictor or advisor in the orchestrator; the four functions already do that on one shared agent.
- [ ] Start and step SUMO in the orchestrator (`SimulationInterface`). Sample every 5 seconds, which is `MonitorConfig.sample_interval_s`. Call `attach_simulation` only after `start()` (the context manager starts it).
- [ ] Call the four functions in the order above, once per sample. A second call at the same simulation time returns the same sample and does not record it again. The detector, predictor and advisor keep their own short history, so the calls have to stay in time order.
- [ ] Treat `"status": "ok"` as success and `"status": "error"` as a failure to log. Do not wrap the four functions in another error format.
- [ ] Do not send the recommendation to SUMO inside TrafficMind. `advice["mode"]` is `"recommend_only"`.
- [ ] Before applying greens, run `validate_plan(program, greens, config)` from `signal_controller.py`. It is the same check the advisor already ran; use it for any plan the orchestrator builds itself. A broken plan raises `UNSAFE_SIGNAL_PLAN` and lists every rule it broke.
- [ ] After a plan is actually installed in SUMO, tell the agent, or the next recommendation is still compared with the old greens. `set_current_plan` rejects an unsafe plan. `scripts/evaluate_signals.py` is the reference for installing greens in SUMO (a test harness, not part of the agent).

```python
from backend.agents.traffic_mind.agent import get_agent
get_agent().advisor.set_current_plan(junction_id, {stage_id: green_seconds})
```
- [ ] Ignore a forecast whose `"method"` is `"persistence"`. That means "assume no change": there is no trained model, or not enough history yet (about 60 seconds). The advisor already ignores those. A real forecast has `"method": "ml"`.
- [ ] Call `reset_agent()` when a simulation ends, and `attach_simulation` again for the next one. Histories must not carry from one run into the next.
- [ ] Keep the unified dashboard in the orchestrator. `visualization.py` is this agent's own Matplotlib view and already consumes these same dicts (via the Pydantic models). Point the shared UI at the four functions rather than reading SUMO again.
- [ ] Leave signal execution, cross-agent priority (emergency vehicles, parking) and the event bus to Phase 2. TrafficMind will not grow those.

## What a recommendation contains

Per junction: `action` is `"keep"` or `"rebalance"`, each stage has `current_green_s`, `recommended_green_s` and `change_s`, and `immediate.action` is one of `none`, `extend_green`, `end_green_early`, `in_clearance`. Yellow and all-red (`yellow_s`, `all_red_s`) are reported and must not be shortened. During clearance the only action is `in_clearance`.

## Failure codes the orchestrator will see

| Code | Usual cause |
|---|---|
| `SIMULATION_NOT_RUNNING` | the four functions were called before `attach_simulation`, or signal advice was requested with no programs loaded |
| `SIMULATION_CONNECTION` / `SUMO_NOT_FOUND` / `SCENARIO_NOT_FOUND` | SUMO failed while the orchestrator was starting or stepping |
| `INVALID_TRAFFIC_DATA` | the snapshot dict does not match the traffic-status schema, or it disagrees with the congestion report |
| `PREDICTION_ERROR` | `horizon_s` is not a positive integer, or nothing has been sampled yet |
| `MODEL_LOAD_ERROR` | a model file does not match this code; retrain with `scripts/train_model.py` |
| `UNSAFE_SIGNAL_PLAN` | `set_current_plan` or `validate_plan` was given greens that break a safety rule |
| `TRAFFICMIND_ERROR` | anything else; the message is the exception text |
