"""Run a TrafficMind scenario and print basic progress.

From the repository root::

    python -m backend.agents.traffic_mind.scripts.run_simulation --until 900
    python -m backend.agents.traffic_mind.scripts.run_simulation --scenario peak --gui
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..config import SimulationConfig, configure_logging
from ..exceptions import TrafficMindError
from ..simulation_interface import SimulationInterface

logger = logging.getLogger("traffic_mind.run_simulation")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a TrafficMind SUMO scenario.")
    parser.add_argument("--scenario", default="mixed",
                        help="offpeak | peak | mixed | surge (default: mixed)")
    parser.add_argument("--gui", action="store_true", help="Open sumo-gui.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scale", type=float, default=1.0, help="Demand multiplier.")
    parser.add_argument("--until", type=int, default=900, help="Stop after this many seconds.")
    parser.add_argument("--report-every", type=int, default=60)
    parser.add_argument("--summary", action="store_true",
                        help="Print the network/signal summary as JSON and exit.")
    args = parser.parse_args(argv)

    configure_logging()
    config = SimulationConfig(
        scenario=args.scenario, gui=args.gui, seed=args.seed, demand_scale=args.scale
    )

    try:
        with SimulationInterface(config) as sim:
            if args.summary:
                print(json.dumps(sim.network_summary(), indent=2))
                return 0
            print(f"{'time_s':>7} {'vehicles':>9} {'halting':>8}")
            while not sim.has_ended and sim.current_time < args.until:
                t = sim.step(args.report_every)
                print(f"{t:7.0f} {sim.vehicle_count():9d} {sim.halting_vehicle_count():8d}")
    except TrafficMindError as exc:
        logger.error("%s: %s", exc.code, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())