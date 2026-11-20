"""Run a scenario and print signal recommendations (advice only; nothing is applied).

From the repository root::

    python -m backend.agents.traffic_mind.scripts.run_signals --scenario surge --until 1800
    python -m backend.agents.traffic_mind.scripts.run_signals --scenario mixed --json-out signals.json

Every ``--report-every`` seconds it prints the junctions where a change is recommended, and the
junction whose recommendation is most urgent in full. The signals in the simulation keep their
normal timing: to see the effect of applying the advice, use ``scripts/evaluate_signals.py``.
"""

from __future__ import annotations

import argparse
import json
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

logger = logging.getLogger("traffic_mind.run_signals")
_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Signal recommendations for a TrafficMind scenario.")
    parser.add_argument("--scenario", default="surge", help="offpeak | peak | mixed | surge")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--until", type=int, default=1800)
    parser.add_argument("--report-every", type=int, default=120)
    parser.add_argument("--no-prediction", action="store_true", help="Use current congestion only.")
    parser.add_argument("--json-out", type=Path, help="Write the last report to this JSON file.")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    interval = MonitorConfig().sample_interval_s
    try:
        with SimulationInterface(SimulationConfig(scenario=args.scenario, seed=args.seed)) as sim:
            monitor = TrafficMonitor.from_source(sim)
            detector = CongestionDetector()
            advisor = SignalAdvisor.from_source(sim)
            predictor = None
            if not args.no_prediction:
                predictor = TrafficPredictor(detector=detector)
                predictor.load_models()
            print(f"{'time_s':>7}  junction  level   now -> forecast   priority  recommendation")
            next_report, report = args.report_every, None
            while not sim.has_ended and sim.current_time < args.until:
                sim.step(interval)
                snapshot = monitor.sample(sim)
                congestion = detector.update(snapshot)
                prediction = predictor.predict(snapshot, congestion) if predictor else None
                report = advisor.recommend(snapshot, congestion, prediction)
                if report.timestamp_s >= next_report:
                    next_report += args.report_every
                    shown = [r for r in report.recommendations if r.action != "keep"
                             or r.immediate.action in ("extend_green", "end_green_early")]
                    shown.sort(key=lambda r: (_ORDER[r.priority], r.junction_id))
                    if not shown:
                        print(f"{report.timestamp_s:7.0f}  no change recommended")
                    for r in shown[:4]:
                        greens = " / ".join(f"{s.current_green_s:.0f}->{s.recommended_green_s:.0f}" for s in r.stages)
                        pred = r.predicted_level.value if r.predicted_level else "-"
                        extra = (f"; now: {r.immediate.action.replace('_', ' ')}"
                                 + (f" +{r.immediate.extend_s:.0f}s" if r.immediate.extend_s else "")
                                 if r.immediate.action in ("extend_green", "end_green_early") else "")
                        print(f"{report.timestamp_s:7.0f}  {r.junction_id:<8}  {r.level.value:<6}  {pred:>6}           "
                              f"{r.priority:<8}  greens {greens} s{extra}")
            if report is not None:
                for note in report.notes:
                    print("Note:", note)
                worst = min(report.recommendations, key=lambda r: (_ORDER[r.priority], r.junction_id), default=None)
                if worst is not None:
                    print(f"\nMost urgent at t={report.timestamp_s:.0f} s: junction {worst.junction_id}\n  {worst.reason}")
                if args.json_out:
                    args.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
                    print(f"Last report written to {args.json_out}")
    except TrafficMindError as exc:
        logger.error("%s: %s", exc.code, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())