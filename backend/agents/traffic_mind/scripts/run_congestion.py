"""Run a scenario and print live congestion levels, alerts and attention junctions.

From the repository root::

    python -m backend.agents.traffic_mind.scripts.run_congestion --until 1800
    python -m backend.agents.traffic_mind.scripts.run_congestion --scenario surge --until 3000 --json-out report.json
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
from ..simulation_interface import SimulationInterface
from ..traffic_monitor import TrafficMonitor

logger = logging.getLogger("traffic_mind.run_congestion")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Detect congestion in a TrafficMind scenario.")
    parser.add_argument("--scenario", default="mixed", help="offpeak | peak | mixed | surge")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--until", type=int, default=1800, help="Stop after this many seconds.")
    parser.add_argument("--interval", type=int, default=MonitorConfig().sample_interval_s,
                        help="Seconds between samples (default 5).")
    parser.add_argument("--report-every", type=int, default=120,
                        help="Print a status line every N simulated seconds.")
    parser.add_argument("--no-alerts", action="store_true",
                        help="Do not print new junction alerts as they happen.")
    parser.add_argument("--json-out", type=Path,
                        help="Write the final congestion report to this JSON file.")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    config = SimulationConfig(scenario=args.scenario, seed=args.seed)

    try:
        with SimulationInterface(config) as sim:
            monitor = TrafficMonitor.from_source(sim)
            detector = CongestionDetector()
            print(f"{'time_s':>7} {'vehicles':>8} {'junctions L/M/H':>16}  attention (rank. junction level score)")
            next_report = args.report_every
            report = None
            while not sim.has_ended and sim.current_time < args.until:
                sim.step(args.interval)
                snapshot = monitor.sample(sim)
                report = detector.update(snapshot)

                if not args.no_alerts:
                    for alert in report.alerts:
                        if alert.is_new and alert.entity_type == "junction":
                            print(f"{report.timestamp_s:7.0f}   ALERT [{alert.severity}] {alert.message}")

                if report.timestamp_s >= next_report:
                    next_report += args.report_every
                    s = report.summary
                    top = ", ".join(
                        f"{a.rank}. {a.junction_id} {a.level.value} {a.score:.2f}"
                        for a in report.attention_junctions[:3]
                    ) or "-"
                    print(f"{report.timestamp_s:7.0f} {snapshot.network.total_vehicles:8d} "
                          f"{s.junctions_low:>6}/{s.junctions_medium}/{s.junctions_high:<6}  {top}")

            if args.json_out and report is not None:
                args.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
                print(f"Final report written to {args.json_out}")
    except TrafficMindError as exc:
        logger.error("%s: %s", exc.code, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())