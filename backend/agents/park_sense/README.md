# ParkSense Agent (TRAFORRA AI)

ParkSense is the intelligent parking management agent of TRAFORRA AI. It works on its own and does not depend on TrafficMind or GreenCorridor.

It does five things:
1. **Occupancy monitoring**: tracks capacity, occupied and available spaces for each parking zone, and detects vehicles entering and leaving (SUMO + TraCI).
2. **Demand prediction**: predicts occupancy 30 minutes ahead and labels demand Low / Medium / High. It uses a rule-based baseline and a Random Forest model.
3. **Smart recommendation**: ranks zones by distance, free spaces and predicted demand (not just the closest zone).
4. **Analytics**: totals, most occupied zone, recommended zone.
5. **Dashboard**: a standalone Matplotlib dashboard.

## Folder structure

```
backend/agents/park_sense/
    config.py                 settings, weights, thresholds, file paths
    schemas.py                data classes and InvalidParkingDataError
    occupancy_monitor.py      entry/exit tracking, SUMO/TraCI reader
    demand_predictor.py       rule-based + ML demand prediction
    recommendation_engine.py  scoring and ranking
    parking_analytics.py      summary statistics
    agent.py                  public interface (use this one)
    dashboard.py              Matplotlib dashboard
    requirements.txt
    tests/                    pytest unit tests
simulation/park_sense/
    scenarios/                SUMO network, parking areas, vehicles, .sumocfg
    parking_data/zones.json   zone names, capacities, coordinates, destinations
datasets/park_sense/
    generate_history.py       creates simulated historical occupancy
    historical_occupancy.csv  generated history (14 days, 15-minute steps)
    demand_model.pkl          trained model (created by --train)
```

## Installation (Windows 11, Cursor or VS Code)

1. Install **SUMO** (64-bit installer from https://sumo.dlr.de/docs/Downloads.php) and keep the `SUMO_HOME` option ticked. Restart the editor.
2. From the project root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend/agents/park_sense/requirements.txt
```

3. Generate data and train the model (one time):

```powershell
python datasets/park_sense/generate_history.py
python -m backend.agents.park_sense.demand_predictor --train
```

## Running

All commands run from the project root.

| Task | Command |
|---|---|
| Run the SUMO scenario in the GUI | `sumo-gui -c simulation/park_sense/scenarios/parksense.sumocfg` |
| Occupancy demo, no SUMO | `python -m backend.agents.park_sense.occupancy_monitor --mode simulated` |
| Occupancy demo, live SUMO | `python -m backend.agents.park_sense.occupancy_monitor --mode sumo` |
| Demand prediction demo | `python -m backend.agents.park_sense.demand_predictor --demo` |
| Recommendation demo | `python -m backend.agents.park_sense.recommendation_engine --demo` |
| Full agent demo, sample data | `python -m backend.agents.park_sense.agent --demo` |
| Full agent demo, live SUMO | `python -m backend.agents.park_sense.agent --sumo` |
| Dashboard, sample data | `python -m backend.agents.park_sense.dashboard --demo` |
| Dashboard, live SUMO | `python -m backend.agents.park_sense.dashboard --sumo` |
| Save dashboard as image | `python -m backend.agents.park_sense.dashboard --demo --save parksense_dashboard.png --no-show` |
| Run all tests | `python -m pytest backend/agents/park_sense/tests -v` |

## Interface for the Central Orchestrator

Import from `backend.agents.park_sense.agent`. Every function returns a JSON-serializable `dict`. Invalid input raises `InvalidParkingDataError` (a subclass of `ValueError`).

```python
from backend.agents.park_sense.agent import get_agent, get_parking_status, recommend_parking
```

**Feeding data.** The module-level functions use one shared agent that starts with all zones empty. Give it live data first, either way:

```python
agent = get_agent()
agent.update_occupancy({"PA_A": 5, "PA_B": 3})   # manual counts (other zones unchanged)
agent.update_from_sumo(sim_time)                  # reads live SUMO through TraCI
```

### `get_parking_status()`
- **Input:** none
- **Output:** `{"timestamp": str, "zones": [{"zone_id", "name", "capacity", "occupied", "available", "occupancy_pct", "status"}]}`; `status` is `"OPEN"` or `"FULL"`.

### `calculate_occupancy(zone_id=None)`
- **Input:** `zone_id` such as `"PA_A"` (optional)
- **Output:** one zone dict (same fields as above), or for the whole city `{"total_capacity", "total_occupied", "total_available", "overall_occupancy_pct"}`.

### `predict_parking_demand(method="auto")`
- **Input:** `method` = `"auto"` (ML if a trained model exists), `"rule_based"` or `"machine_learning"`
- **Output:** `{"timestamp", "horizon_minutes", "predictions": [{"zone_id", "current_occupancy_pct", "predicted_occupancy_pct", "demand_level", "method", "horizon_minutes"}]}`; `demand_level` is `"Low"` (< 50%), `"Medium"` (50% to < 80%) or `"High"` (>= 80%).

### `recommend_parking(destination=None, top_n=3)`
- **Input:** `destination` as `"hospital"`, `{"x": 400, "y": 200, "name": "..."}` or `(400, 200)`; `top_n` (positive integer). Default destination is the hospital.
- **Output:**

```json
{
  "destination": {"name": "City Hospital", "x": 400.0, "y": 200.0},
  "recommendations": [
    {"rank": 1, "zone_id": "PA_B", "name": "City Mall Parking", "available_spaces": 9,
     "distance_m": 250.0, "eta_seconds": 40.6, "demand_level": "Low",
     "score": 0.915, "reason": "most free spaces (9); demand expected to stay low"}
  ],
  "best": { "...": "same as recommendations[0], or null if nothing is available" },
  "excluded": [{"zone_id": "PA_X", "name": "...", "reason": "No free spaces"}],
  "message": "Recommended: City Mall Parking (PA_B)"
}
```

### `get_parking_analytics(destination=None)`
- **Input:** same destination formats as above
- **Output:** `{"timestamp", "destination", "total_capacity", "total_occupied", "total_available", "overall_occupancy_pct", "city_demand_level", "demand_summary": {"Low", "Medium", "High"}, "most_occupied_zone": {...}, "recommended_zone": {...} or null, "zones": [per-zone status + predicted_occupancy_pct + demand_level]}`

## How recommendations are scored

`score = 0.40 x distance + 0.35 x availability + 0.25 x demand` (weights in `config.py`)

- Distance score: 1.0 at 0 m, falling to 0 at 2000 m.
- Availability score: free spaces / 10, capped at 1.0.
- Demand score: Low = 1.0, Medium = 0.5, High = 0.0.
- Zones with no free spaces are excluded and listed with a reason.

## Notes and limitations

- Distances are straight-line from the zone's coordinates; travel time adds a 30% detour allowance. It does not use real road routing.
- The training history is simulated (`generate_history.py`). With real data, put a CSV with the same columns in `datasets/park_sense/` and re-run `--train`.
- Prediction accuracy is printed by `--train`: it compares the ML model with the rule-based baseline on held-out data.
- If no trained model exists, ParkSense automatically uses the rule-based method.

## Branch

Developed on the Git branch `Parksense`. Integration into `main` happens in Phase 2 together with the other agents.