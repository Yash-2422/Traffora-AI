# TrafficMind - input and output reference

What goes into the TrafficMind agent and what comes out. Everything below is checked by the test suite: the example files in [`examples/`](examples) are real outputs and `tests/test_documentation_examples.py` validates them against the schemas, so this page cannot silently drift from the code.

## Conventions

- **JSON-serialisable.** Every result is a Pydantic model; call `.to_dict()` to get plain JSON-ready data.
- **Times** are simulation seconds from the start of the run (`timestamp_s`). **Lengths** are metres, **speeds** m/s, **waiting times** seconds, **flows** vehicles per hour.
- **Levels** are the strings `"LOW"`, `"MEDIUM"`, `"HIGH"` (score below 0.25 / 0.25 to 0.50 / 0.50 and above).
- **Ids** are SUMO ids: junctions `A0` ... `C2` (letter = column, digit = row), roads like `B1A1` (from `B1` to `A1`).
- **Success** results carry `"status": "ok"`. **Failures** raise a `TrafficMindError` whose `.to_dict()` is `{"status": "error", "code": ..., "message": ...}` (codes at the end of this page).
- **Strict schemas.** Unknown fields and impossible values (negative counts, NaN, a queue longer than its road) are rejected.
- **Recommend only.** TrafficMind never changes a signal. The orchestrator decides whether to apply a recommendation (Phase 2).

## How to produce each output

The four outputs carry the names of the public functions in the project brief (`get_traffic_status`, `detect_congestion`, `predict_congestion`, `generate_signal_recommendation`). Those names become one-line wrapper functions in `agent.py` in the integration step (Step 10); until then use the classes below, which do the actual work.

| Output | Produced by | Needs |
|---|---|---|
| 1. traffic status | `TrafficMonitor.sample(sim)` (or `.update(raw_observation)`) | a running `SimulationInterface`, or your own `RawObservation` |
| 2. congestion | `CongestionDetector.update(status)` | the traffic status |
| 3. forecast | `TrafficPredictor.predict(status, congestion)` | the two above, in time order, one call per sample; a trained model (falls back to "no change" without one) |
| 4. signal advice | `SignalAdvisor.recommend(status, congestion, forecast)` | the above; the junctions' signal programs (`SignalAdvisor.from_source(sim)`); the forecast is optional |

Call them once per sample (every 5 simulated seconds), in time order: the detector, predictor and advisor all keep short histories.

```python
from backend.agents.traffic_mind.config import SimulationConfig
from backend.agents.traffic_mind.simulation_interface import SimulationInterface
from backend.agents.traffic_mind.traffic_monitor import TrafficMonitor
from backend.agents.traffic_mind.congestion_detector import CongestionDetector
from backend.agents.traffic_mind.traffic_predictor import TrafficPredictor
from backend.agents.traffic_mind.signal_controller import SignalAdvisor

with SimulationInterface(SimulationConfig(scenario="surge")) as sim:
    monitor = TrafficMonitor.from_source(sim)
    detector = CongestionDetector()
    predictor = TrafficPredictor(detector=detector)
    predictor.load_models()
    advisor = SignalAdvisor.from_source(sim)
    while sim.current_time < 1800:
        sim.step(5)
        status = monitor.sample(sim)                                # 1. traffic status
        congestion = detector.update(status)                        # 2. congestion
        forecast = predictor.predict(status, congestion)            # 3. forecast
        advice = advisor.recommend(status, congestion, forecast)    # 4. signal advice
    print(advice.to_dict())
```

---

## The four outputs

### 1. `get_traffic_status()` - current traffic metrics

Result type: `TrafficSnapshot` (`backend/agents/traffic_mind/schemas.py`). Full real example: [`examples/traffic_status.json`](examples/traffic_status.json).

**`TrafficSnapshot`**

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | Always `"ok"` for a successful result (errors use the error object, see the end). |
| `timestamp_s` | `float` (>= 0) | Simulation time, seconds. |
| `network` | `NetworkMetrics` | Whole-network totals. |
| `junctions` | `List[JunctionMetrics]` | One entry per signalised junction. |
| `edges` | `List[EdgeMetrics]` | One entry per road. |

**`NetworkMetrics`**

| Field | Type | Meaning |
|---|---|---|
| `total_vehicles` | `int` (>= 0) | Vehicles in the network. |
| `halting_vehicles` | `int` (>= 0) | Stopped vehicles in the network. |
| `mean_waiting_time_s` | `float` (>= 0) | Mean waiting time of the vehicles on the roads (waiting is summed over the last 5 min of each vehicle). |
| `mean_speed_ratio` | `float` (>= 0, <= 1) | Mean speed divided by the speed limit, 0-1. |

**`JunctionMetrics`**

| Field | Type | Meaning |
|---|---|---|
| `junction_id` | `str` | Junction id (e.g. `B1`). |
| `incoming_edges` | `List[str]` | Roads that lead into the junction. |
| `vehicle_count` | `int` (>= 0) | Vehicles on the incoming roads. |
| `queue_length_veh` | `int` (>= 0) | Stopped vehicles on the incoming roads. |
| `max_queue_length_m` | `float` (>= 0) | Longest queue among the incoming roads, metres. |
| `mean_waiting_time_s` | `float` (>= 0) | Vehicle-weighted mean waiting time on the incoming roads, seconds. |
| `max_occupancy_pct` | `float` (>= 0, <= 100) | Highest road occupancy among the incoming roads, 0-100. |
| `flow_veh_per_hour` | `float` (>= 0) | Vehicles entering the incoming roads, per hour (rolling 60 s window), summed. |
| `current_phase` | `int]` (>= 0) | Index of the running signal phase (null if unknown). |
| `phase_remaining_s` | `float]` (>= 0) | Seconds until that phase ends (null if unknown). |

**`EdgeMetrics`**

| Field | Type | Meaning |
|---|---|---|
| `edge_id` | `str` | Road id. |
| `junction_id` | `str]` | Junction the road leads into (null for roads that leave the network). |
| `length_m` | `float` (> 0) | Road length, metres. |
| `lane_count` | `int` (>= 1) | Number of lanes. |
| `speed_limit_mps` | `float` (> 0) | Speed limit, m/s. |
| `vehicle_count` | `int` (>= 0) | Vehicles on the road now. |
| `density_veh_per_km` | `float` (>= 0) | Vehicles per kilometre of road (all lanes). |
| `density_veh_per_km_lane` | `float` (>= 0) | Vehicles per kilometre per lane. |
| `occupancy_pct` | `float` (>= 0, <= 100) | Share of the road length covered by vehicles, 0-100. |
| `queue_length_veh` | `int` (>= 0) | Stopped vehicles. |
| `queue_length_m` | `float` (>= 0) | Queue length from the stop line, metres (never more than the road length). |
| `mean_waiting_time_s` | `float` (>= 0) | Mean waiting time of the vehicles on the road, seconds. |
| `mean_speed_mps` | `float` (>= 0) | Mean speed, m/s. |
| `speed_ratio` | `float` (>= 0, <= 1) | Mean speed divided by the speed limit, 0-1. |
| `flow_veh_per_hour` | `float` (>= 0) | Vehicles entering the road per hour (rolling 60 s window). |

Example (surge scenario, t = 1800 s; lists shortened to one item):

```json
{
  "status": "ok",
  "timestamp_s": 1800.0,
  "network": {
    "total_vehicles": 163,
    "halting_vehicles": 67,
    "mean_waiting_time_s": 4.89,
    "mean_speed_ratio": 0.4075
  },
  "junctions": [
    {
      "junction_id": "A1",
      "incoming_edges": [
        "A0A1",
        "A2A1",
        "B1A1",
        "left1A1"
      ],
      "vehicle_count": 36,
      "queue_length_veh": 24,
      "max_queue_length_m": 238.55,
      "mean_waiting_time_s": 11.08,
      "max_occupancy_pct": 52.364,
      "flow_veh_per_hour": 1020.0,
      "current_phase": 3,
      "phase_remaining_s": 15.0
    }
  ],
  "edges": [
    {
      "edge_id": "B1A1",
      "junction_id": "A1",
      "length_m": 235.6,
      "lane_count": 1,
      "speed_limit_mps": 13.89,
      "vehicle_count": 6,
      "density_veh_per_km": 25.467,
      "density_veh_per_km_lane": 25.467,
      "occupancy_pct": 13.171,
      "queue_length_veh": 5,
      "queue_length_m": 38.54,
      "mean_waiting_time_s": 6.17,
      "mean_speed_mps": 2.238,
      "speed_ratio": 0.1611,
      "flow_veh_per_hour": 120.0
    }
  ]
}
```

### 2. `detect_congestion()` - congestion levels, alerts and attention list

Result type: `CongestionReport` (`backend/agents/traffic_mind/schemas.py`). Full real example: [`examples/congestion.json`](examples/congestion.json).

**`CongestionReport`**

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `"ok"` for a successful result. |
| `timestamp_s` | `float` | Simulation time, seconds. |
| `summary` | `CongestionSummary` | Counts of junctions and roads per level. |
| `junctions` | `List[JunctionCongestion]` | One entry per junction. |
| `edges` | `List[EdgeCongestion]` | One entry per road. |
| `alerts` | `List[CongestionAlert]` | Active alerts (junctions and roads at Medium or High). |
| `attention_junctions` | `List[AttentionJunction]` | Junctions that need attention, most urgent first. |

**`CongestionSummary`**

| Field | Type | Meaning |
|---|---|---|
| `junctions_low` | `int` | Junctions at Low. |
| `junctions_medium` | `int` | Junctions at Medium. |
| `junctions_high` | `int` | Junctions at High. |
| `roads_low` | `int` | Roads at Low. |
| `roads_medium` | `int` | Roads at Medium. |
| `roads_high` | `int` | Roads at High. |

**`JunctionCongestion`**

| Field | Type | Meaning |
|---|---|---|
| `junction_id` | `str` | Junction id. |
| `score` | `float` (>= 0, <= 1) | Score of the worst approach road, 0-1. |
| `raw_level` | `<enum 'CongestionLevel` | Level of the current score alone. |
| `level` | `<enum 'CongestionLevel` | Reported level after the persistence rule. **Use this one.** |
| `since_s` | `float` | Simulation time at which the current level started. |
| `worst_edge_id` | `str]` | The approach road with the highest score. |
| `dominant_factor` | `str` | Signal that contributes most on the worst road. |
| `factors` | `CongestionFactors` | The four signals of the worst road. |
| `vehicle_count` | `int` | Vehicles on the incoming roads. |
| `queue_length_veh` | `int` | Stopped vehicles on the incoming roads. |
| `max_queue_length_m` | `float` | Longest queue, metres. |
| `mean_waiting_time_s` | `float` | Vehicle-weighted mean waiting time, seconds. |
| `requires_attention` | `bool` | True if the junction is on the attention list. |
| `attention_reason` | `str]` | Why (null if not flagged). |

**`EdgeCongestion`**

| Field | Type | Meaning |
|---|---|---|
| `edge_id` | `str` | Road id. |
| `junction_id` | `str]` | Junction the road leads into (null if none). |
| `score` | `float` (>= 0, <= 1) | Congestion score 0-1 (weighted mix of the four factors, averaged over the last 70 s). |
| `raw_level` | `<enum 'CongestionLevel` | Level of the current score alone. |
| `level` | `<enum 'CongestionLevel` | Reported level after the persistence rule (a change must last 15 s up / 30 s down). **Use this one.** |
| `factors` | `CongestionFactors` | The four signals behind the score. |
| `dominant_factor` | `str` | Which signal contributes most (`occupancy`, `queue`, `waiting` or `speed_drop`). |
| `vehicle_count` | `int` | Vehicles on the road. |
| `queue_length_veh` | `int` | Stopped vehicles. |
| `queue_length_m` | `float` | Queue length, metres. |

**`CongestionFactors`**

| Field | Type | Meaning |
|---|---|---|
| `occupancy` | `float` (>= 0, <= 1) | Occupancy signal, 0-1 (1 = fully congested). |
| `queue` | `float` (>= 0, <= 1) | Queue-length signal, 0-1. |
| `waiting` | `float` (>= 0, <= 1) | Waiting-time signal, 0-1. |
| `speed_drop` | `float` (>= 0, <= 1) | Speed-drop signal, 0-1. |

**`CongestionAlert`**

| Field | Type | Meaning |
|---|---|---|
| `alert_id` | `str` | Stable id: stays the same while the alert stays active. |
| `entity_type` | `Literal['junction', 'road']` | `junction` or `road`. |
| `entity_id` | `str` | Junction or road id. |
| `level` | `<enum 'CongestionLevel` | `MEDIUM` or `HIGH`. |
| `severity` | `Literal['warning', 'critical']` | `warning` (Medium) or `critical` (High). |
| `score` | `float` | Current score. |
| `since_s` | `float` | When the level started. |
| `is_new` | `bool` | True only in the update where the level was first reached. |
| `message` | `str` | Readable description. |

**`AttentionJunction`**

| Field | Type | Meaning |
|---|---|---|
| `rank` | `int` (>= 1) | 1 = needs attention most. |
| `junction_id` | `str` | Junction id. |
| `level` | `<enum 'CongestionLevel` | Reported level. |
| `score` | `float` | Current score. |
| `reason` | `str` | Readable reason. |
| `worst_edge_id` | `str]` | Worst approach road. |
| `max_queue_length_m` | `float` | Longest queue, metres. |
| `mean_waiting_time_s` | `float` | Mean waiting time, seconds. |

Example (surge scenario, t = 1800 s; lists shortened to one item):

```json
{
  "status": "ok",
  "timestamp_s": 1800.0,
  "summary": {
    "junctions_low": 3,
    "junctions_medium": 4,
    "junctions_high": 2,
    "roads_low": 41,
    "roads_medium": 5,
    "roads_high": 2
  },
  "junctions": [
    {
      "junction_id": "A1",
      "score": 0.8099,
      "raw_level": "HIGH",
      "level": "HIGH",
      "since_s": 1465.0,
      "worst_edge_id": "left1A1",
      "dominant_factor": "queue",
      "factors": {
        "occupancy": 1.0,
        "queue": 1.0,
        "waiting": 0.1128,
        "speed_drop": 0.9365
      },
      "vehicle_count": 36,
      "queue_length_veh": 24,
      "max_queue_length_m": 238.55,
      "mean_waiting_time_s": 11.08,
      "requires_attention": true,
      "attention_reason": "HIGH congestion for 335 s"
    }
  ],
  "edges": [
    {
      "edge_id": "B1A1",
      "junction_id": "A1",
      "score": 0.3629,
      "raw_level": "MEDIUM",
      "level": "MEDIUM",
      "factors": {
        "occupancy": 0.2636,
        "queue": 0.2871,
        "waiting": 0.138,
        "speed_drop": 0.8445
      },
      "dominant_factor": "speed_drop",
      "vehicle_count": 6,
      "queue_length_veh": 5,
      "queue_length_m": 38.54
    }
  ],
  "alerts": [
    {
      "alert_id": "junction:A1:HIGH:1465",
      "entity_type": "junction",
      "entity_id": "A1",
      "level": "HIGH",
      "severity": "critical",
      "score": 0.8099,
      "since_s": 1465.0,
      "is_new": false,
      "message": "Junction A1: HIGH congestion (score 0.81, mainly queue length on left1A1, averaged over the last 70 s). Right now: 24 stopped vehicles, longest queue 239 m, mean wait 11 s."
    }
  ],
  "attention_junctions": [
    {
      "rank": 1,
      "junction_id": "A1",
      "level": "HIGH",
      "score": 0.8099,
      "reason": "HIGH congestion for 335 s",
      "worst_edge_id": "left1A1",
      "max_queue_length_m": 238.55,
      "mean_waiting_time_s": 11.08
    }
  ]
}
```

### 3. `predict_congestion()` - congestion forecast

Result type: `PredictionReport` (`backend/agents/traffic_mind/schemas.py`). Full real example: [`examples/prediction.json`](examples/prediction.json).

**`PredictionReport`**

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `"ok"` for a successful result. |
| `timestamp_s` | `float` (>= 0) | Simulation time the forecast was made, seconds. |
| `horizon_s` | `int` (> 0) | How far ahead the forecast looks, seconds (30, 60 or 120 with the shipped models). |
| `method` | `Literal['ml', 'persistence']` | `ml` (trained model) or `persistence` (fallback: assume nothing changes). |
| `model` | `ModelInfo]` | Details of the model used (null when `method` is `persistence`). |
| `history_available_s` | `float` (>= 0) | Seconds of history the predictor has seen (it needs 60 s). |
| `predictions` | `List[JunctionPrediction]` | One entry per junction. |
| `early_warnings` | `List[EarlyWarning]` | Junctions expected to get worse, most severe first. |
| `notes` | `List[str]` | Plain-language notes, e.g. why the fallback was used. |

**`ModelInfo`**

| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | Model type (`gradient_boosting`, `random_forest` or `logistic_regression`). |
| `horizon_s` | `int` | Horizon the model was trained for. |
| `feature_count` | `int` | Number of input features. |
| `trained_at` | `str]` | UTC time of training. |
| `training_rows` | `int]` | Rows used for training. |
| `training_runs` | `int]` | Simulation runs used for training. |
| `test_macro_f1` | `float]` | Macro-F1 on runs the model never saw. |

**`JunctionPrediction`**

| Field | Type | Meaning |
|---|---|---|
| `junction_id` | `str` | Junction id. |
| `current_level` | `<enum 'CongestionLevel` | Reported level now. |
| `current_score` | `float` (>= 0, <= 1) | Congestion score now, 0-1. |
| `predicted_level` | `<enum 'CongestionLevel` | Level expected `horizon_s` seconds ahead. |
| `probabilities` | `Dict[str, float]` | Probability of each level (`LOW`, `MEDIUM`, `HIGH`); adds up to 1. |
| `confidence` | `float` (>= 0, <= 1) | Probability of the predicted level. |
| `expected_change` | `Literal['worsening', 'steady', 'improving']` | `worsening`, `steady` or `improving` compared with now. |
| `method` | `Literal['ml', 'persistence']` | `ml` or `persistence`. |

**`EarlyWarning`**

| Field | Type | Meaning |
|---|---|---|
| `junction_id` | `str` | Junction id. |
| `current_level` | `<enum 'CongestionLevel` | Level now. |
| `predicted_level` | `<enum 'CongestionLevel` | Level expected. |
| `confidence` | `float` (>= 0, <= 1) | Probability of the predicted level. |
| `message` | `str` | Readable warning. |

Example (surge scenario, t = 1800 s; lists shortened to one item):

```json
{
  "status": "ok",
  "timestamp_s": 1800.0,
  "horizon_s": 60,
  "method": "ml",
  "model": {
    "name": "gradient_boosting",
    "horizon_s": 60,
    "feature_count": 35,
    "trained_at": "2026-11-20T06:28:49+00:00",
    "training_rows": 62640,
    "training_runs": 30,
    "test_macro_f1": 0.9
  },
  "history_available_s": 1795.0,
  "predictions": [
    {
      "junction_id": "A0",
      "current_level": "LOW",
      "current_score": 0.179,
      "predicted_level": "MEDIUM",
      "probabilities": {
        "LOW": 0.3187,
        "MEDIUM": 0.6811,
        "HIGH": 0.0002
      },
      "confidence": 0.6811,
      "expected_change": "worsening",
      "method": "ml"
    }
  ],
  "early_warnings": [
    {
      "junction_id": "B2",
      "current_level": "MEDIUM",
      "predicted_level": "HIGH",
      "confidence": 0.9945,
      "message": "Junction B2: MEDIUM now, HIGH expected in 60 s (99% confidence)."
    }
  ],
  "notes": []
}
```

### 4. `generate_signal_recommendation()` - signal timing advice (recommend only)

Result type: `SignalRecommendationReport` (`backend/agents/traffic_mind/schemas.py`). Full real example: [`examples/signal_recommendation.json`](examples/signal_recommendation.json).

**`SignalRecommendationReport`**

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `"ok"` for a successful result. |
| `mode` | `Literal['recommend_only']` | Always `recommend_only`: nothing is executed by TrafficMind. |
| `timestamp_s` | `float` (>= 0) | Simulation time, seconds. |
| `summary` | `SignalSummary` | Counts. |
| `recommendations` | `List[JunctionSignalRecommendation]` | One entry per junction that has a signal program. |
| `notes` | `List[str]` | Plain-language notes (e.g. junction skipped, no prediction supplied). |

**`SignalSummary`**

| Field | Type | Meaning |
|---|---|---|
| `junctions_rebalance` | `int` | Junctions where a green-time change is recommended. |
| `junctions_keep` | `int` | Junctions where the plan stays as it is. |
| `immediate_actions` | `int` | Junctions with an extend / end-early action for the running green. |

**`JunctionSignalRecommendation`**

| Field | Type | Meaning |
|---|---|---|
| `junction_id` | `str` | Junction id. |
| `level` | `<enum 'CongestionLevel` | Congestion level now. |
| `predicted_level` | `CongestionLevel]` | Forecast level (null if no trained-model forecast was supplied). |
| `priority` | `Literal['none', 'low', 'medium', 'high']` | `none`, `low`, `medium` or `high` (from the worse of now and forecast). |
| `action` | `Literal['keep', 'rebalance']` | `rebalance` (change the green times as listed in `stages`) or `keep`. |
| `stages` | `List[StagePlan]` | One entry per stage (green phase), with current and recommended green. |
| `yellow_s` | `float` | Yellow time per stage. Never changed. |
| `all_red_s` | `float` | All-red time per stage. Never changed. |
| `cycle_s` | `float` | Cycle length with the recommended greens, seconds. |
| `current_phase` | `int]` | Index of the running phase (null if unknown). |
| `phase_elapsed_s` | `float]` | Seconds the running phase has lasted (null if unknown). |
| `immediate` | `ImmediateAction` | What to do with the phase running now. |
| `reason` | `str` | Plain-language explanation. |
| `safety` | `SafetyCheck` | Result of the safety check on this recommendation. |

**`StagePlan`**

| Field | Type | Meaning |
|---|---|---|
| `stage_id` | `int` | Stage number (1 = first green phase of the junction's program). |
| `edges` | `List[str]` | Roads that get green in this stage. |
| `demand` | `float` (>= 0, <= 1) | Highest congestion score among those roads, 0-1. |
| `current_green_s` | `float` | Green seconds in the plan that is running. |
| `recommended_green_s` | `float` | Recommended green seconds. |
| `change_s` | `float` | Recommended minus current. |

**`ImmediateAction`**

| Field | Type | Meaning |
|---|---|---|
| `action` | `Literal['none', 'extend_green', 'end_green_early', 'in_clearance']` | `none`, `extend_green`, `end_green_early` or `in_clearance` (yellow / all-red running: nothing may change). |
| `extend_s` | `float` (>= 0) | Seconds to add to the running green (only for `extend_green`). |
| `reason` | `str` | Plain-language explanation. |

**`SafetyCheck`**

| Field | Type | Meaning |
|---|---|---|
| `validated` | `bool` | True if the recommendation passed every safety rule. |
| `rules_checked` | `List[str]` | Names of the rules that were checked. |

Example (surge scenario, t = 1800 s; lists shortened to one item):

```json
{
  "status": "ok",
  "mode": "recommend_only",
  "timestamp_s": 1800.0,
  "summary": {
    "junctions_rebalance": 5,
    "junctions_keep": 4,
    "immediate_actions": 2
  },
  "recommendations": [
    {
      "junction_id": "A1",
      "level": "HIGH",
      "predicted_level": "HIGH",
      "priority": "high",
      "action": "rebalance",
      "stages": [
        {
          "stage_id": 1,
          "edges": [
            "A0A1",
            "A2A1"
          ],
          "demand": 0.059,
          "current_green_s": 30.0,
          "recommended_green_s": 20.0,
          "change_s": -10.0
        },
        {
          "stage_id": 2,
          "edges": [
            "B1A1",
            "left1A1"
          ],
          "demand": 0.8099,
          "current_green_s": 30.0,
          "recommended_green_s": 40.0,
          "change_s": 10.0
        }
      ],
      "yellow_s": 3.0,
      "all_red_s": 2.0,
      "cycle_s": 70.0,
      "current_phase": 3,
      "phase_elapsed_s": 15.0,
      "immediate": {
        "action": "none",
        "extend_s": 0.0,
        "reason": ""
      },
      "reason": "Stage 2 (B1A1, left1A1) is the busiest (score 0.81 vs 0.06); green time moves toward it: stage 1 (A0A1, A2A1): 30 s -> 20 s; stage 2 (B1A1, left1A1): 30 s -> 40 s. The cycle length stays the same.",
      "safety": {
        "validated": true,
        "rules_checked": [
          "every_stage_served",
          "min_green",
          "max_green",
          "yellow_not_shortened",
          "all_red_not_shortened",
          "max_cycle",
          "max_change_per_step"
        ]
      }
    }
  ],
  "notes": []
}
```

---

## Input accepted by the monitor (for data that does not come from SUMO)

`TrafficMonitor.update()` takes a `RawObservation` (or an equivalent dict). `SimulationInterface.read_observation()` produces one from SUMO; any other source can produce the same shape.

**`RawObservation`**

| Field | Type | Meaning |
|---|---|---|
| `time_s` | `float` (>= 0) | Simulation time of the observation, seconds (>= 0). |
| `total_vehicles` | `int` (>= 0) | Vehicles in the whole network. |
| `edges` | `List[RawEdgeObservation]` | One entry per road (`RawEdgeObservation`). |
| `signals` | `List[RawSignalObservation]` | One entry per signalised junction (`RawSignalObservation`); may be empty. |

**`RawEdgeObservation`**

| Field | Type | Meaning |
|---|---|---|
| `edge_id` | `str` | Road id. |
| `vehicle_ids` | `List[str]` | Ids of the vehicles on the road (optional; if given, its length must equal `vehicle_count`). Used to measure flow. |
| `vehicle_count` | `int` (>= 0) | Vehicles on the road now. |
| `halting_count` | `int` (>= 0) | Vehicles that are stopped (< 0.1 m/s). Cannot exceed `vehicle_count`. |
| `occupancy_pct` | `float` (>= 0, <= 100) | Share of the road length covered by vehicles, 0-100. |
| `mean_speed_mps` | `float` (>= 0) | Mean speed, m/s (the speed limit when the road is empty). |
| `waiting_time_sum_s` | `float` (>= 0) | Waiting time summed over the vehicles on the road, seconds. |
| `queue_length_m` | `float` (>= 0) | Length of the queue measured from the stop line, metres. |

**`RawSignalObservation`**

| Field | Type | Meaning |
|---|---|---|
| `junction_id` | `str` | Junction id. |
| `phase_index` | `int` (>= 0) | Index of the running signal phase. |
| `phase_remaining_s` | `float` (>= 0) | Seconds until the phase changes. |

## Input accepted by the signal advisor

`SignalAdvisor` needs each junction's fixed signal program. `SignalAdvisor.from_source(sim)` reads them from SUMO (`SimulationInterface.read_signal_programs()`); for any other source pass a dict `{junction_id: RawSignalProgram}`.

**`RawSignalProgram`**

| Field | Type | Meaning |
|---|---|---|
| `junction_id` | `str` | Junction id. |
| `phases` | `List[RawSignalPhase]` | The phases of the fixed program, in order. |
| `link_edges` | `List[str]]` | For each signal link (by index), the road it controls (null if none). Phase states have one character per link. |

**`RawSignalPhase`**

| Field | Type | Meaning |
|---|---|---|
| `state` | `str` | SUMO signal state string, one character per link (`G`/`g` green, `y` yellow, `r` red). |
| `duration_s` | `float` (> 0) | Phase duration, seconds (> 0). |


The advisor splits a program into **stages**: one green phase plus the yellow / all-red phases that follow it. Stage 1 is the first green phase in the program.

## Levels, forecasts and fallbacks

| Situation | What you get |
|---|---|
| No trained model for the requested horizon | `method: "persistence"`: every junction is predicted to stay at its current level, with a note saying so |
| Less than 60 s of history | same fallback, with a note |
| A forecast of `method: "persistence"` passed to the signal advisor | ignored (noted); advice uses current congestion only |
| Model trained with other features / thresholds / scikit-learn version | refused (`MODEL_LOAD_ERROR`) / a note in `notes` |

## Signal safety rules

Every recommendation is checked before it is returned, and `validate_plan(program, greens, config, previous_greens)` applies the same check to any plan the orchestrator builds. A plan is rejected (`UNSAFE_SIGNAL_PLAN`, listing every broken rule) unless:

| Rule | Default |
|---|---|
| every stage gets green in every cycle (none skipped or starved) | - |
| green is at least | 15 s |
| green is at most | 45 s |
| yellow and all-red are not shorter than the program's own (and the floors) | floors 3 s / 2 s |
| the cycle is at most | 90 s |
| a green changes by at most (per recommendation) | 10 s |

The phase order is never changed. While yellow or all-red is running the only possible action is `in_clearance`: nothing changes. The limits live in `SignalConfig` (`config.py`).

## Error codes

| Code | Raised when |
|---|---|
| `SIMULATION_CONNECTION` | SUMO could not be started, or the connection was lost |
| `SUMO_NOT_FOUND` | no SUMO executable found (install SUMO, set `SUMO_HOME`) |
| `SCENARIO_NOT_FOUND` | the `.sumocfg` does not exist (run `build_scenario.py`) |
| `SIMULATION_NOT_RUNNING` | an operation needs a running simulation |
| `INVALID_TRAFFIC_DATA` | malformed or out-of-range input, or inputs from different times |
| `UNSAFE_SIGNAL_PLAN` | a signal plan breaks a safety rule |
| `PREDICTION_ERROR` | a forecast could not be made (no data yet, invalid horizon, the model failed) |
| `MODEL_LOAD_ERROR` | a model file is missing, unreadable, or does not match this code |
| `TRAFFICMIND_ERROR` | base class of all of the above |

```json
{"status": "error", "code": "INVALID_TRAFFIC_DATA", "message": "Snapshot (t=100.0) and congestion report (t=50.0) are from different times."}
```