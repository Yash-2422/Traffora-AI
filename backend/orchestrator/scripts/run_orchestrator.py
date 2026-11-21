"""Run the Traffora-AI orchestrator against the unified SUMO scenario.

Examples (Windows PowerShell)
------------------------------
    python -m backend.orchestrator.scripts.run_orchestrator
    python -m backend.orchestrator.scripts.run_orchestrator --end 600
    python -m backend.orchestrator.scripts.run_orchestrator --gui
    python -m backend.orchestrator.scripts.run_orchestrator --no-signal-control --json-out runs/fixed_time.json

The script prints *measured* values only: everything comes from TraCI and from
the agents' own outputs. Nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

# Make ``backend`` importable when the script is run directly.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.orchestrator import (  # noqa: E402
    OrchestratorConfig,
    TrafforaOrchestrator,
    configure_logging,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Traffora-AI central orchestrator.")
    parser.add_argument("--sumocfg", type=Path, default=None, help="Override the .sumocfg file.")
    parser.add_argument("--end", type=int, default=None, help="Stop at this simulation time (s).")
    parser.add_argument("--seed", type=int, default=None, help="SUMO random seed.")
    parser.add_argument("--gui", action="store_true", help="Run sumo-gui instead of sumo.")
    parser.add_argument("--step-length", type=float, default=None, help="SUMO step length (s).")
    parser.add_argument("--scale", type=float, default=None, help="SUMO demand multiplier (1.0 = as written).")
    parser.add_argument(
        "--decision-interval", type=float, default=None, help="Seconds between agent sampling rounds."
    )
    parser.add_argument(
        "--no-signal-control",
        action="store_true",
        help="Disable signal writes (fixed-time baseline; agents still observe).",
    )
    parser.add_argument(
        "--no-parking", action="store_true", help="Disable the ParkSense adapter."
    )
    parser.add_argument(
        "--no-emergency", action="store_true", help="Disable the GreenCorridor adapter."
    )
    parser.add_argument("--json-out", type=Path, default=None, help="Write the final report as JSON.")
    parser.add_argument("--quiet", action="store_true", help="Only warnings and errors.")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> OrchestratorConfig:
    """Build a config from CLI flags (``OrchestratorConfig`` is frozen)."""
    overrides: dict = {
        "gui": bool(args.gui),
        "enable_signal_control": not args.no_signal_control,
        "enable_park_sense": not args.no_parking,
        "enable_green_corridor": not args.no_emergency,
    }
    if args.sumocfg is not None:
        overrides["sumocfg"] = args.sumocfg
    if args.end is not None:
        overrides["end_time"] = args.end
    if args.seed is not None:
        overrides["seed"] = args.seed
    if args.step_length is not None:
        overrides["step_length"] = args.step_length
    if args.scale is not None:
        overrides["demand_scale"] = args.scale
    if args.decision_interval is not None:
        overrides["decision_interval_s"] = args.decision_interval
    return dataclasses.replace(OrchestratorConfig(), **overrides)


def run(config: OrchestratorConfig) -> dict:
    """Run the simulation to ``config.end_time`` and return the measured report."""
    orchestrator = TrafforaOrchestrator(config)
    orchestrator.initialize()
    orchestrator.start()
    try:
        while orchestrator.coordinator.is_running:
            orchestrator.run_step()
            now = orchestrator.state.simulation_time_s
            if int(now) % 60 == 0:
                metrics = orchestrator.state.metrics()
                logging.getLogger("run_orchestrator").info(
                    "t=%.0fs vehicles=%s waiting=%.1fs queue=%.2f parking=%.1f%% decisions=%d",
                    now,
                    metrics.vehicles_departed,
                    metrics.average_waiting_time_s or 0.0,
                    metrics.average_queue_length_m or 0.0,
                    metrics.parking_occupancy_pct or 0.0,
                    len(orchestrator.state.get_decisions()),
                )
    finally:
        report = build_report(orchestrator, config)
        orchestrator.shutdown()
    return report


def build_report(orchestrator: TrafforaOrchestrator, config: OrchestratorConfig) -> dict:
    metrics = orchestrator.get_simulation_metrics()
    decisions = orchestrator.get_decisions(limit=10_000)
    applied = [d for d in decisions if d.applied and d.signal_actions]
    rejected = [d for d in decisions if not d.applied and d.signal_actions]
    return {
        "config": {
            "sumocfg": str(config.resolved_sumocfg()),
            "seed": config.seed,
            "end_time": config.end_time,
            "signal_control": config.enable_signal_control,
            "traffic_mind": config.enable_traffic_mind,
            "green_corridor": config.enable_green_corridor,
            "park_sense": config.enable_park_sense,
        },
        "metrics": metrics.model_dump(mode="json"),
        "agents": [s.model_dump(mode="json") for s in orchestrator.get_agent_statuses()],
        "decisions": {
            "total": len(decisions),
            "applied_signal_decisions": len(applied),
            "rejected_signal_decisions": len(rejected),
            "by_type": _count_by(decisions, "decision_type"),
            "rejection_reasons": sorted({d.rejected_reason for d in rejected if d.rejected_reason}),
        },
        "events": orchestrator.events.stats(),
        "latest_decision": (
            orchestrator.get_latest_decision().model_dump(mode="json")
            if orchestrator.get_latest_decision()
            else None
        ),
        "parking": orchestrator.get_parking_analytics(),
        "traffic": orchestrator.get_traffic_analytics(),
    }


def _count_by(decisions: list, field: str) -> dict:
    counts: dict[str, int] = {}
    for decision in decisions:
        key = getattr(decision, field)
        key = getattr(key, "value", key)
        counts[str(key)] = counts.get(str(key), 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(logging.WARNING if args.quiet else logging.INFO)
    config = build_config(args)

    print("Traffora-AI orchestrator")
    print(f"  scenario : {config.resolved_sumocfg()}")
    print(f"  seed     : {config.seed}")
    print(f"  end time : {config.end_time}s")
    print(f"  control  : {'enabled' if config.enable_signal_control else 'disabled (baseline)'}")
    print()

    report = run(config)

    metrics = report["metrics"]
    print("\n=== Measured results ===")
    print(f"  simulated time            : {metrics.get('simulation_time_s'):.0f}s")
    print(f"  vehicles departed/arrived : {metrics.get('vehicles_departed')} / {metrics.get('vehicles_arrived')}")
    print(f"  average waiting time      : {_fmt(metrics.get('average_waiting_time_s'))}s")
    print(f"  average queue length      : {_fmt(metrics.get('average_queue_length_m'))} veh/edge")
    print(f"  average travel time       : {_fmt(metrics.get('average_travel_time_s'))}s")
    print(f"  emergency travel time     : {_fmt(metrics.get('emergency_vehicle_travel_time_s'))}s")
    print(f"  emergency priority resp.  : {_fmt(metrics.get('emergency_priority_response_time_s'))}s")
    print(f"  parking occupancy         : {_fmt(metrics.get('parking_occupancy_pct'))}%")
    print(f"  accepted / rejected       : {metrics.get('accepted_decisions')} / {metrics.get('rejected_decisions')}")
    print(f"  processing latency        : {_fmt(metrics.get('orchestrator_processing_latency_ms'))}ms")
    print(f"  agent availability        : {_fmt(metrics.get('agent_availability_pct'))}%")

    print("\n=== Agents ===")
    for agent in report["agents"]:
        print(f"  {agent['agent']:<14} {agent['health']:<10} {agent['message']}")

    print("\n=== Decisions ===")
    print(f"  total                     : {report['decisions']['total']}")
    print(f"  applied signal decisions  : {report['decisions']['applied_signal_decisions']}")
    print(f"  rejected signal decisions : {report['decisions']['rejected_signal_decisions']}")
    for key, value in sorted(report["decisions"]["by_type"].items()):
        print(f"    {key:<24}: {value}")
    for reason in report["decisions"]["rejection_reasons"]:
        print(f"    rejected: {reason}")

    print("\n=== Events ===")
    for key, value in sorted(report["events"].items()):
        print(f"  {key:<24}: {value}")

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {args.json_out}")

    return 0


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
