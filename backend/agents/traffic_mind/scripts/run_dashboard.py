"""Run a scenario and draw the TrafficMind dashboard (Matplotlib).

From the repository root::

    python -m backend.agents.traffic_mind.scripts.run_dashboard --scenario surge --until 3000
    python -m backend.agents.traffic_mind.scripts.run_dashboard --scenario mixed --live
    python -m backend.agents.traffic_mind.scripts.run_dashboard --scenario surge --theme dark --junction A1 --save out.png

Without ``--live`` the run is recorded and one PNG is written at the end (no window needed).
With ``--live`` a window opens and updates while the simulation runs; close it or press Ctrl+C
to stop, and the PNG is still written. The signals in the simulation keep their normal timing
(TrafficMind only recommends).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ..config import MonitorConfig, SimulationConfig, configure_logging
from ..congestion_detector import CongestionDetector
from ..exceptions import TrafficMindError
from ..signal_controller import SignalAdvisor
from ..simulation_interface import SimulationInterface
from ..traffic_monitor import TrafficMonitor
from ..traffic_predictor import TrafficPredictor
from ..visualization import LiveDashboard, RunRecorder, save_dashboard

logger = logging.getLogger("traffic_mind.run_dashboard")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Draw the TrafficMind dashboard for a scenario.")
    parser.add_argument("--scenario", default="surge", help="offpeak | peak | mixed | surge")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--until", type=int, default=3000, help="Stop after this many simulated seconds.")
    parser.add_argument("--live", action="store_true", help="Open a window that updates during the run.")
    parser.add_argument("--live-every", type=int, default=15, help="Redraw the live window every N simulated seconds.")
    parser.add_argument("--save", type=Path, default=Path("traffic_mind_dashboard.png"), help="PNG to write at the end.")
    parser.add_argument("--junction", help="Junction for the forecast panel (default: the busiest).")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--csv-dir", type=Path, help="Also write the recorded data (network.csv, junctions.csv) here.")
    parser.add_argument("--no-prediction", action="store_true", help="Skip forecasts (and the forecast panel).")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    interval = MonitorConfig().sample_interval_s
    recorder = RunRecorder(scenario=args.scenario)
    live = None
    try:
        with SimulationInterface(SimulationConfig(scenario=args.scenario, seed=args.seed)) as sim:
            monitor = TrafficMonitor.from_source(sim)
            detector = CongestionDetector()
            advisor = SignalAdvisor.from_source(sim)
            predictor = None
            if not args.no_prediction:
                predictor = TrafficPredictor(detector=detector)
                if not predictor.load_models():
                    print("No trained model found: the forecast panel will be empty. "
                          "Train one with scripts/train_model.py.")
            if args.live:
                live = LiveDashboard(recorder, theme=args.theme, show=True)
            next_draw = 0
            print(f"Running {args.scenario} for {args.until} s ...")
            while not sim.has_ended and sim.current_time < args.until:
                sim.step(interval)
                snapshot = monitor.sample(sim)
                congestion = detector.update(snapshot)
                prediction = predictor.predict(snapshot, congestion) if predictor else None
                signals = advisor.recommend(snapshot, congestion, prediction)
                recorder.add(snapshot, congestion, prediction, signals)
                if live is not None and sim.current_time >= next_draw:
                    next_draw = sim.current_time + args.live_every
                    live.refresh()
                    if not live._plt.fignum_exists(live.fig.number):     # the window was closed
                        print("Window closed: stopping.")
                        break
    except TrafficMindError as exc:
        logger.error("%s: %s", exc.code, exc)
        return 1
    except KeyboardInterrupt:
        print("Interrupted: saving what was recorded.")
    finally:
        if live is not None:
            live.close()

    if len(recorder) == 0:
        logger.error("Nothing was recorded.")
        return 1
    path = save_dashboard(recorder, args.save, junction=args.junction, theme=args.theme)
    print(f"Dashboard saved to {path.resolve()}")
    if args.csv_dir:
        for csv in recorder.save_csv(args.csv_dir):
            print(f"Data saved to {csv.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())