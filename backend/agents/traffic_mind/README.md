# TrafficMind

The traffic agent of **VELORA AI**. It watches a simulated city grid, detects congestion, forecasts where congestion is about to appear, and recommends signal timing changes. It is **standalone**: it does not depend on GreenCorridor, ParkSense or the Central Orchestrator, and it only *recommends* (the orchestrator will decide whether to apply anything in Phase 2).

![TrafficMind dashboard, surge scenario](../../../docs/traffic_mind/images/dashboard_surge_light.png)

| Capability | Where | What you get |
|---|---|---|
| Traffic monitoring | `traffic_monitor.py` | density, occupancy, queue length, waiting time, speed, flow per road and junction |
| Congestion detection | `congestion_detector.py` | LOW / MEDIUM / HIGH per road and junction, alerts, an attention list |
| Congestion forecast | `traffic_predictor.py` | the level each junction will have 30, 60 or 120 s ahead, with probabilities |
| Signal recommendations | `signal_controller.py` | safe green-time advice, never touching yellow or all-red |
| Dashboard | `visualization.py` | a static six-panel figure and a live window (Matplotlib) |

Input and output formats, with real examples: [`docs/traffic_mind/INPUT_OUTPUT.md`](../../../docs/traffic_mind/INPUT_OUTPUT.md).

---

## 1. Install (Windows 11, VS Code, Python 3.10)

You need Python 3.10 (verified with 3.10.11) and SUMO 1.28.

1. **Python.** In a terminal: `python --version` should print 3.10.x.
2. **SUMO.** Install SUMO 1.28 from <https://sumo.dlr.de/docs/Downloads.php>. Set the `SUMO_HOME` environment variable to the SUMO folder (the one that contains `bin\sumo.exe`), then **restart VS Code / the terminal**:
   ```powershell
   setx SUMO_HOME "C:\Program Files (x86)\SUMO"
   ```
   Check with `sumo --version` (should say 1.28.x).
3. **Virtual environment and packages** (from the repository root):
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r backend/agents/traffic_mind/requirements.txt
   ```
   If PowerShell refuses to run the activate script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
4. **Scenarios** are committed (`simulation/traffic_mind/`). To rebuild them: `python simulation/traffic_mind/build_scenario.py`.
5. **Check everything works:**
   ```powershell
   python -m pytest backend/agents/traffic_mind/tests -q
   ```
   Everything should pass (288 tests at the time of writing, 1-2 minutes). Tests that need SUMO are skipped, not failed, if SUMO is missing.

All commands below run from the repository root.

## 2. Run it

| What | Command |
|---|---|
| Run a scenario, print a vehicle table | `python -m backend.agents.traffic_mind.scripts.run_simulation --scenario mixed --until 600` |
| Same with the SUMO window | `... run_simulation --scenario mixed --gui` |
| Live traffic metrics | `... scripts.run_monitor --scenario mixed --until 1800` |
| Congestion levels, alerts, attention list | `... scripts.run_congestion --scenario surge --until 3000` |
| Congestion forecasts, checked against what happened | `... scripts.run_prediction --scenario surge --until 3000` |
| Signal recommendations | `... scripts.run_signals --scenario surge --until 1800 --report-every 300` |
| Dashboard PNG (static) | `... scripts.run_dashboard --scenario surge --until 3000` |
| Dashboard window (live) | `... scripts.run_dashboard --scenario mixed --live` |
| Does following the advice help? (test harness) | `... scripts.evaluate_signals --scenarios surge mixed --seeds 42 43` |

(`...` stands for `python -m backend.agents.traffic_mind`; add `.scripts.` as shown.) Add `--help` to any script for its options. The dashboard PNG is written to `datasets/traffic_mind/output/` (ignored by git); `--save`, `--theme dark`, `--junction A1` and `--csv-dir` change that.

The four scenarios (one simulated hour each, on a 3x3 grid of signalised junctions): `offpeak` (light traffic), `peak` (heavy all hour), `mixed` (two directional rush periods, a surge, then recovery; the default) and `surge` (calm network hit by a local surge).

Messages like `Retrying in 1 seconds` while SUMO starts are normal.

## 3. How it works

```
SUMO --TraCI--> SimulationInterface --> TrafficMonitor --> CongestionDetector --> TrafficPredictor --> SignalAdvisor
                (only file that                  (pure Python on Pydantic models; no SUMO from here on)
                 talks to SUMO)                                  |                      |                  |
                                                                 +---------- RunRecorder / dashboard ------+
```

Only `simulation_interface.py` imports TraCI. Everything after it works on plain data models, so each stage can be tested, replayed or fed from another source without SUMO.

**Monitoring.** Every 5 simulated seconds: per road the vehicle count, density, occupancy, stopped vehicles, queue length in metres (a chain of slow cars from the stop line; handles queues that are discharging), mean waiting time, speed ratio and flow; per junction the aggregates and the signal phase; and network totals.

**Congestion detection.** Four signals per road, each scaled 0-1: occupancy, queue length, waiting time and speed drop. They are weighted (0.25 / 0.35 / 0.20 / 0.20), averaged over 70 s so that one signal cycle does not look like a jam, and turned into a score. Score under 0.25 is LOW, 0.25-0.50 MEDIUM, 0.50 and above HIGH. A new level must persist (15 s going up, 30 s going down) before it is reported, which stops flicker. A junction takes the score of its worst approach road.

**Forecast.** For each junction a classifier predicts the level the detector will report 30, 60 or 120 s later. Inputs are 35 numbers: current metrics, how they changed over the last 30 and 60 s, the score trend, and network-wide context. It was trained on 40 randomly varied simulation runs (about 93,000 rows); training, test and the four named scenarios are separate runs, so the scores below are on traffic the model never saw. Gradient boosting won for 30 and 60 s, random forest for 120 s, chosen by cross-validation over runs.

| Horizon | Method | Macro-F1 | Worsening caught in advance | False alarms |
|---|---|---|---|---|
| 30 s | **trained model** | **0.97** | **91%** | 1.3% |
| | score rule | 0.96 | 50% | 0.2% |
| | "nothing changes" | 0.86 | 0% | 0% |
| 60 s | **trained model** | **0.90** | **85%** | 4.3% |
| | score rule | 0.82 | 24% | 0.6% |
| | "nothing changes" | 0.76 | 0% | 0% |
| 120 s | **trained model** | **0.74** | **61%** | 10.3% |
| | score rule | 0.68 | 12% | 1.6% |
| | "nothing changes" | 0.66 | 0% | 0% |

Held-out random runs (10 runs, about 21,000 rows per horizon). The four named scenarios tell the same story (60 s: 0.89 vs 0.79 vs 0.71). Full numbers, confusion matrices and per-scenario results: `models/metrics_h*.json`. On a live surge run the 60 s forecast matched the level reached 92.7% of the time against 82.8% for "nothing changes".

*What the columns mean.* Macro-F1 averages how well each of the three levels is predicted (1 = perfect). "Worsening caught in advance" is the share of moments where congestion was about to get worse that the model warned about. False alarms are the share of other moments where it warned anyway. Longer horizons are harder: at 120 s the model catches more than half of the onsets but raises more false alarms, so treat 120 s forecasts as a hint.

*About the "score rule".* This baseline was meant to extrapolate the score trend, but its tuned extrapolation gain came out as 0 for every horizon, so it reduces to "classify the current score with the thresholds, without the persistence delay". It still beats "nothing changes" because the reported level lags the score. The trained model beats both.

**Signal recommendations.** Each junction's program is split into *stages* (a green phase plus its yellow and all-red). For each junction the advisor looks at how busy each stage's roads are and suggests moving up to 10 s of green toward the busier stage (cycle length unchanged), and, for the phase running now, extending a green that is about to end while its traffic is heavy, or ending an idle green early when another stage waits. It keeps quiet when traffic is light or evenly heavy.

Safety rules, checked on every recommendation and available to the orchestrator as `validate_plan()`: every stage gets green every cycle; green stays within 15-45 s; yellow and all-red are never shortened; the phase order never changes; the cycle stays at or below 90 s; a green changes by at most 10 s per step. During yellow and all-red nothing can change. The limits are in `SignalConfig`.

*Does the advice help?* To find out, `scripts/evaluate_signals.py` plays the orchestrator inside a simulation, applies every recommendation, and compares with the normal fixed timing (average of two seeds, first 3000 s):

| Scenario | Stopped vehicles | Junction time at HIGH | Mean waiting time | Trips finished |
|---|---|---|---|---|
| mixed | -40% | -89% | -21% | +1.8% |
| surge | -12% | -21% | -8% | +3.4% |
| peak | -4% | -49% | -5% | +0.4% |
| offpeak | +2% | no HIGH either way | +4% | -0.1% |

Large gains where traffic is uneven, small gains under uniform heavy load, nothing to gain (and a difference within run-to-run noise) when traffic is light. An audit of the simulated signals (`tests/test_signal_integration.py`) confirms that with every recommendation applied, no yellow or all-red phase was shortened, the phase order never changed and no green left the 15-45 s range.

**Dashboard.** `visualization.py` records the agent's outputs (`RunRecorder`) and draws the map, traffic over time, congestion and green-shift heatmaps, forecast against actual for a junction, and time spent congested. Congestion levels use fixed status colors and are always also shown by shape and label. Light and dark themes. The unified web dashboard is built at integration (Phase 2); this one is the module's own view.

## 4. Configuration

All tunable numbers are in `config.py`, none are hard-coded in the logic:

| Class | What it controls |
|---|---|
| `SimulationConfig` | scenario, seed, demand scale, queue measurement |
| `MonitorConfig` | sample interval (5 s), flow window, history size |
| `CongestionConfig` | factor weights, reference values, thresholds, smoothing, persistence |
| `PredictionConfig` | horizons, lags, trend window, history needed |
| `SignalConfig` | min / max green, max change, max cycle, clearance floors, rebalancing and extension rules |

## 5. Data and models

- `datasets/traffic_mind/sample_traffic_dataset.csv`: a small sample (one run, 1,062 rows, one row per junction every 30 s) so you can see the training data without generating it.
- `models/traffic_model_h30|60|120.joblib` and `models/metrics_h*.json`: the trained models and their evaluation (about 7.6 MB together). The model files are pickles: only load files you trust.
- To regenerate the data and retrain (about 5 minutes for data, 3-5 minutes for training):
  ```powershell
  python -m backend.agents.traffic_mind.scripts.generate_dataset --random-runs 40 --workers 2
  python -m backend.agents.traffic_mind.scripts.train_model --horizons 30 60 120
  ```
  The full dataset (`traffic_dataset.csv`, about 30 MB) is ignored by git.
- The models were trained with scikit-learn 1.7. If your installed version differs, the forecast carries a note and you should retrain.

## 6. Tests

`python -m pytest backend/agents/traffic_mind/tests -q`. 288 tests: units for the monitor, queue measurement, detector (persistence, alerts, attention list), features, baselines and training, the predictor and its fallbacks, signal stages, the safety validator and recommendations, the dashboard (drawn off screen), and the documentation examples; plus integration tests that run SUMO (monitoring, forecasts against what happened, signals audited second by second). The signal safety checks include a randomized test of 300 situations.

## 7. Limits to know about

- **One synthetic network.** Everything was built and trained on a 3x3 grid with single-lane roads and fixed-time signals. A model trained here will not transfer to another network without retraining.
- **The forecast predicts our detector's output.** Labels come from `CongestionDetector`, so the model forecasts what the detector will report, not an independent ground truth.
- **Signal evaluation is small.** Two seeds per scenario; read the table as encouraging, not proven. Applying recommendations in a real network would also need the orchestrator's own safeguards.
- **Recommend only.** Nothing here changes a signal. `evaluate_signals.py` applies advice only inside a test simulation.
- **Windows live window.** The live dashboard needs a graphical Matplotlib backend (the default on a normal Windows Python install works).

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `SUMO_NOT_FOUND` | Install SUMO, set `SUMO_HOME`, restart the terminal / VS Code. |
| `SCENARIO_NOT_FOUND` | Run `python simulation/traffic_mind/build_scenario.py`. |
| `Could not connect in 4 tries` | A leftover `sumo.exe` may be holding things up: end it in Task Manager and retry. |
| Fewer tests than expected | A test file is not inside `backend/agents/traffic_mind/tests/`. |
| `MODEL_LOAD_ERROR` | The model does not match this code: retrain with `scripts/train_model.py`. |
| No forecasts / "No trained model" | Models folder missing or empty: retrain, or restore `models/`. |

## 9. Git

Work stays on the TrafficMind branch and inside the `traffic_mind` paths (`backend/agents/traffic_mind/`, `simulation/traffic_mind/`, `datasets/traffic_mind/`, `docs/traffic_mind/`). Do not commit `.venv/`, `__pycache__/`, `datasets/traffic_mind/traffic_dataset.csv` or `datasets/traffic_mind/output/` (the last two are ignored already).