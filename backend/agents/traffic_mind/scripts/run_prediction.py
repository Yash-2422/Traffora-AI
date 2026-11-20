"""Run a scenario and print congestion forecasts, then check them against what happened.

From the repository root::

    python -m backend.agents.traffic_mind.scripts.run_prediction --scenario mixed --until 3000
    python -m backend.agents.traffic_mind.scripts.run_prediction --scenario surge --horizon 120 --json-out forecast.json

Needs a trained model (``scripts/train_model.py``); without one it says so and predicts
"no change". At the end it prints how often the forecast matched the level reached
``horizon`` seconds later (the same scoring as in training, on this one run).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from ..config import MonitorConfig, PredictionConfig, SimulationConfig, configure_logging
from ..congestion_detector import CongestionDetector
from ..exceptions import TrafficMindError
from ..simulation_interface import SimulationInterface
from ..traffic_monitor import TrafficMonitor
from ..traffic_predictor import TrafficPredictor

logger = logging.getLogger("traffic_mind.run_prediction")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Forecast congestion in a TrafficMind scenario.")
    parser.add_argument("--scenario", default="mixed", help="offpeak | peak | mixed | surge")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--until", type=int, default=3000)
    parser.add_argument("--horizon", type=int, default=PredictionConfig().horizon_s,
                        choices=PredictionConfig().horizons_s, help="Seconds ahead to predict.")
    parser.add_argument("--report-every", type=int, default=120)
    parser.add_argument("--json-out", type=Path, help="Write the last prediction report to this file.")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    interval = MonitorConfig().sample_interval_s
    config = SimulationConfig(scenario=args.scenario, seed=args.seed)
    levels: Dict[Tuple[str, int], str] = {}                 # (junction, time) -> reported level
    forecasts: List[Tuple[str, int, str, str]] = []         # (junction, target time, now, predicted)
    try:
        predictor = TrafficPredictor()
        loaded = predictor.load_models()
        if args.horizon not in loaded:
            print(f"No trained model for {args.horizon} s in the models folder: forecasts will be "
                  "'no change'. Train one with scripts/train_model.py.")
        with SimulationInterface(config) as sim:
            monitor = TrafficMonitor.from_source(sim)
            detector = CongestionDetector()
            print(f"{'time_s':>7} {'now L/M/H':>10}  early warnings (junction now -> expected in {args.horizon} s, confidence)")
            next_report, report = args.report_every, None
            while not sim.has_ended and sim.current_time < args.until:
                sim.step(interval)
                snapshot = monitor.sample(sim)
                congestion = detector.update(snapshot)
                report = predictor.predict(snapshot, congestion, horizon_s=args.horizon)
                t = int(round(report.timestamp_s))
                for p in report.predictions:
                    levels[(p.junction_id, t)] = p.current_level.value
                    if report.method == "ml":
                        forecasts.append((p.junction_id, t + args.horizon,
                                          p.current_level.value, p.predicted_level.value))
                if report.timestamp_s >= next_report:
                    next_report += args.report_every
                    s = congestion.summary
                    warn = ", ".join(f"{w.junction_id} {w.current_level.value}->{w.predicted_level.value} "
                                     f"({w.confidence:.0%})" for w in report.early_warnings[:4]) or "-"
                    print(f"{report.timestamp_s:7.0f} {s.junctions_low:>3}/{s.junctions_medium}/{s.junctions_high:<4}  {warn}")
            if report is not None:
                for note in report.notes:
                    print("Note:", note)
                if args.json_out:
                    args.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
                    print(f"Last prediction report written to {args.json_out}")
    except TrafficMindError as exc:
        logger.error("%s: %s", exc.code, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 0

    scored = [(now, pred, levels[(j, t)]) for j, t, now, pred in forecasts if (j, t) in levels]
    if scored:
        hits = sum(pred == actual for _, pred, actual in scored)
        stay = sum(now == actual for now, _, actual in scored)
        print(f"\nChecked {len(scored)} forecasts against what happened {args.horizon} s later:")
        print(f"  model forecast was right {hits / len(scored):.1%} of the time")
        print(f"  'nothing changes' would have been right {stay / len(scored):.1%} of the time")
    return 0


if __name__ == "__main__":
    sys.exit(main())