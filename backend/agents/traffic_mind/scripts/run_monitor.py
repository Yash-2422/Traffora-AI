"""Run a scenario and print live traffic metrics from the TrafficMonitor.

From the repository root::

    python -m backend.agents.traffic_mind.scripts.run_monitor --until 900
    python -m backend.agents.traffic_mind.scripts.run_monitor --until 1500 --json-out snapshot.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ..config import MonitorConfig, SimulationConfig, configure_logging
from ..exceptions import TrafficMindError
from ..simulation_interface import SimulationInterface
from ..traffic_monitor import TrafficMonitor

logger = logging.getLogger("traffic_mind.run_monitor")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Monitor traffic in a TrafficMind scenario.")
    parser.add_argument("--scenario", default="mixed")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--until", type=int, default=900, help="Stop after this many seconds.")
    parser.add_argument("--interval", type=int, default=MonitorConfig().sample_interval_s,
                        help="Seconds between samples (default 5).")
    parser.add_argument("--report-every", type=int, default=60,
                        help="Print a line every N simulated seconds.")
    parser.add_argument("--json-out", type=Path,
                        help="Write the final snapshot to this JSON file.")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    config = SimulationConfig(scenario=args.scenario, seed=args.seed)

    try:
        with SimulationInterface(config) as sim:
            monitor = TrafficMonitor.from_source(sim)
            print(f"{'time_s':>7} {'vehicles':>8} {'halting':>7} {'avg_wait_s':>10} "
                  f"{'speed_ratio':>11}  busiest junction (vehicles / queue / wait s)")
            next_report = args.report_every
            snapshot = None
            while not sim.has_ended and sim.current_time < args.until:
                sim.step(args.interval)
                snapshot = monitor.sample(sim)
                if snapshot.timestamp_s >= next_report:
                    next_report += args.report_every
                    net = snapshot.network
                    top = max(snapshot.junctions, key=lambda j: j.vehicle_count)
                    print(f"{snapshot.timestamp_s:7.0f} {net.total_vehicles:8d} "
                          f"{net.halting_vehicles:7d} {net.mean_waiting_time_s:10.1f} "
                          f"{net.mean_speed_ratio:11.2f}  "
                          f"{top.junction_id} ({top.vehicle_count} / {top.queue_length_veh} / "
                          f"{top.mean_waiting_time_s:.0f})")
            if args.json_out and snapshot is not None:
                args.json_out.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
                print(f"Final snapshot written to {args.json_out}")
    except TrafficMindError as exc:
        logger.error("%s: %s", exc.code, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())