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