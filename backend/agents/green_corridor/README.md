# GreenCorridor Agent (Phase 1, standalone)

## Windows 11 + VS Code setup
1. Install SUMO (1.18+) from https://sumo.dlr.de/docs/Downloads.php (Windows MSI). Default path: `C:\Program Files (x86)\Eclipse\Sumo`.
2. Environment variables (Win+R -> `sysdm.cpl` -> Advanced -> Environment Variables):
   - `SUMO_HOME` = `C:\Program Files (x86)\Eclipse\Sumo`
   - append `%SUMO_HOME%\bin` to `Path`. Restart VS Code afterwards.
3. In the VS Code terminal (PowerShell), from the repo root, on branch `yash-greencorridor`:
   ```
   git checkout yash-greencorridor
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements-greencorridor.txt
   ```
   If PowerShell blocks activation: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
   If you did NOT install the MSI, `eclipse-sumo` from pip supplies SUMO itself, and no env vars are needed.
4. Verify: `python simulation/green_corridor/verify_setup.py` (expect `10/10 checks passed`).
5. Tests: `python -m pytest backend/agents/green_corridor/tests -v`.

A "Retrying in 1 seconds" line while TraCI connects is normal.

## TraCI interaction plan
- `connection.py` owns the SUMO process and TraCI handle (one labelled connection; `step()` detects crashes).
- Each step the agent reads: `vehicle` (position, speed, edge, route), `simulation.findRoute` (routing),
  `trafficlight` (state, phase, program, next switch). It writes signals only via the validated executor.

## Safe signal priority workflow
Request -> validate against live state (not yellow/all-red, min green elapsed, target phase exists in the
junction's own program, no conflicting active request) -> switch only through the program's own
yellow/clearance sequence -> ambulance passes -> restore request -> normal program resumes.
Custom phase strings are never written.

## Step 3: build the scenario (run inside WSL/Ubuntu if Windows blocks SUMO executables)
```
python -m backend.agents.green_corridor.scenario_builder          # defaults: 5x5 grid, 300 background cars, seed 42
python -m backend.agents.green_corridor.scenario_builder --seed 7 --background-vehicles 500 --ambulance-depart 90
```# GreenCorridor Agent (Phase 1, standalone)

## Windows 11 + VS Code setup
1. Install SUMO (1.18+) from https://sumo.dlr.de/docs/Downloads.php (Windows MSI). Default path: `C:\Program Files (x86)\Eclipse\Sumo`.
2. Environment variables (Win+R -> `sysdm.cpl` -> Advanced -> Environment Variables):
   - `SUMO_HOME` = `C:\Program Files (x86)\Eclipse\Sumo`
   - append `%SUMO_HOME%\bin` to `Path`. Restart VS Code afterwards.
3. In the VS Code terminal (PowerShell), from the repo root, on branch `yash-greencorridor`:
   ```
   git checkout yash-greencorridor
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements-greencorridor.txt
   ```
   If PowerShell blocks activation: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
   If you did NOT install the MSI, `eclipse-sumo` from pip supplies SUMO itself, and no env vars are needed.
4. Verify: `python simulation/green_corridor/verify_setup.py` (expect `10/10 checks passed`).
5. Tests: `python -m pytest backend/agents/green_corridor/tests -v`.

A "Retrying in 1 seconds" line while TraCI connects is normal.

## TraCI interaction plan
- `connection.py` owns the SUMO process and TraCI handle (one labelled connection; `step()` detects crashes).
- Each step the agent reads: `vehicle` (position, speed, edge, route), `simulation.findRoute` (routing),
  `trafficlight` (state, phase, program, next switch). It writes signals only via the validated executor.

## Safe signal priority workflow
Request -> validate against live state (not yellow/all-red, min green elapsed, target phase exists in the
junction's own program, no conflicting active request) -> switch only through the program's own
yellow/clearance sequence -> ambulance passes -> restore request -> normal program resumes.
Custom phase strings are never written.

## Step 3: build the scenario (run inside WSL/Ubuntu if Windows blocks SUMO executables)
```
python -m backend.agents.green_corridor.scenario_builder          # defaults: 5x5 grid, 300 background cars, seed 42
python -m backend.agents.green_corridor.scenario_builder --seed 7 --background-vehicles 500 --ambulance-depart 90
```
Generates `simulation/green_corridor/sample_network/grid.net.xml`, `scenarios/*.xml`, `baseline.sumocfg`,
`priority.sumocfg` and `scenario.json` (read by the agent in later steps). Edge ids: node `A0` -> `A1` is edge `A0A1`.
Same seed -> identical traffic, so baseline and priority runs are directly comparable.
Add `simulation/green_corridor/results/` to `.gitignore` (SUMO writes tripinfo files there).

## Step 4/5: detection and route planning (Python API)
```python
from backend.agents.green_corridor.connection import SumoConnection
from backend.agents.green_corridor.emergency_detector import EmergencyDetector, detect_emergency_vehicle, get_emergency_status
from backend.agents.green_corridor.route_planner import RoutePlanner, calculate_emergency_route, check_emergency_route, recalculate_emergency_route

with SumoConnection() as sc:
    sc.start("simulation/green_corridor/scenarios/priority.sumocfg")
    det, planner = EmergencyDetector(sc), RoutePlanner(sc)
    while sc.traci.simulation.getMinExpectedNumber() > 0:
        sc.step()
        detect_emergency_vehicle(det)                       # call every step
        r = get_emergency_status(det, "amb_1")              # {"success", "data", "error", "timestamp"}
```
All public functions return `{"success": bool, "data": {...}|null, "error": {"code","message"}|null, "timestamp"}` and never raise.

Route planning is read-only: it builds a NetworkX graph of the live SUMO network and weights it with current
per-edge travel times (harmonic mean of vehicle speeds, capped at 5x free-flow). Closed roads are excluded and
U-turns are not planned. Estimated times exclude waiting at signals; the real journey time is measured from SUMO.
Generates `simulation/green_corridor/sample_network/grid.net.xml`, `scenarios/*.xml`, `baseline.sumocfg`,
`priority.sumocfg` and `scenario.json` (read by the agent in later steps). Edge ids: node `A0` -> `A1` is edge `A0A1`.
Same seed -> identical traffic, so baseline and priority runs are directly comparable.
Add `simulation/green_corridor/results/` to `.gitignore` (SUMO writes tripinfo files there).