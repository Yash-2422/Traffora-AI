"""Run the Traffora-AI demonstration scenario and narrate what actually happens.

This script drives the real SUMO scenario through the real orchestrator and
prints a timeline built **only from measured state**: agent health, agent
recommendations, decisions, safety verdicts and TraCI metrics. It never prints
a canned "success" line -- if an agent produces nothing, the timeline says so.

Timeline it reports (whatever the simulation actually does):

    normal traffic -> congestion detected -> parking pressure + alternative
    -> ambulance detected -> emergency priority -> safety validation
    -> signal applied -> ambulance passes -> traffic optimisation resumes

Usage (Windows PowerShell)
--------------------------
    python -m backend.orchestrator.scripts.run_demo
    python -m backend.orchestrator.scripts.run_demo --end 900 --scale 2
    python -m backend.orchestrator.scripts.run_demo --json-out runs/demo.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.orchestrator import (  # noqa: E402
    DecisionType,
    OrchestratorConfig,
    TrafforaOrchestrator,
    configure_logging,
)
from backend.orchestrator.schemas import ValidationStatus  # noqa: E402

logger = logging.getLogger("run_demo")


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Traffora-AI demonstration scenario.")
    parser.add_argument("--end", type=int, default=900, help="Simulation end time (s).")
    parser.add_argument("--scale", type=float, default=2.0, help="Demand multiplier.")
    parser.add_argument("--seed", type=int, default=42, help="SUMO random seed.")
    parser.add_argument("--gui", action="store_true", help="Run sumo-gui.")
    parser.add_argument("--json-out", type=Path, default=None, help="Write the timeline as JSON.")
    parser.add_argument("--quiet", action="store_true", help="Only warnings and errors.")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> OrchestratorConfig:
    return dataclasses.replace(
        OrchestratorConfig(),
        end_time=args.end,
        demand_scale=args.scale,
        seed=args.seed,
        gui=args.gui,
        label="traffora-demo",
    )


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(logging.WARNING if args.quiet else logging.ERROR)
    config = build_config(args)

    print("=" * 78)
    print(" Traffora-AI -- multi-agent urban mobility demonstration")
    print("=" * 78)
    print(f" scenario : {config.resolved_sumocfg()}")
    print(f" demand   : scale {config.demand_scale}, seed {config.seed}, end {config.end_time}s")
    print()

    orchestrator = TrafforaOrchestrator(config)
    timeline: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {}

    orchestrator.initialize()
    orchestrator.start()
    try:
        print("-- Agents initialised against the shared SUMO session " + "-" * 22)
        for status in orchestrator.get_agent_statuses():
            print(f"   {status.agent.value:<14} {status.health.value:<10} {status.message}")

        print()
        print("-- Control loop " + "-" * 60)

        seen_congestion = False
        seen_parking = False
        seen_emergency = False
        seen_emergency_cleared = False
        seen_traffic_resumed = False

        while orchestrator.coordinator.is_running:
            now = orchestrator.coordinator.current_time
            decisions = orchestrator.run_step()

            for decision in decisions:
                # --- congestion ------------------------------------------- #
                if (decision.decision_type is DecisionType.TRAFFIC_OPTIMIZATION
                        and not seen_congestion):
                    seen_congestion = True
                    timeline.append({
                        "t": now,
                        "event": "congestion_detected",
                        "explanation": decision.explanation,
                        "applied": decision.applied,
                        "tls": [a.tls_id for a in decision.signal_actions],
                    })
                    print(f"   t={now:6.0f}s  CONGESTION   {decision.explanation}")

                # --- parking ---------------------------------------------- #
                if (decision.decision_type is DecisionType.PARKING_RECOMMENDATION
                        and not seen_parking):
                    seen_parking = True
                    best = decision.parking_recommendations[0] if decision.parking_recommendations else None
                    timeline.append({
                        "t": now,
                        "event": "parking_recommendation",
                        "explanation": decision.explanation,
                        "recommended_zone": best.zone_id if best else None,
                        "available": best.available if best else None,
                    })
                    print(f"   t={now:6.0f}s  PARKING      {decision.explanation}")

                # --- emergency -------------------------------------------- #
                if (decision.decision_type is DecisionType.EMERGENCY_PRIORITY
                        and not seen_emergency):
                    seen_emergency = True
                    validation = decision.validation
                    timeline.append({
                        "t": now,
                        "event": "emergency_priority",
                        "explanation": decision.explanation,
                        "applied": decision.applied,
                        "tls": [a.tls_id for a in decision.signal_actions],
                        "safety_status": validation.status.value if validation else None,
                        "safety_reasons": validation.reasons if validation else [],
                    })
                    print(f"   t={now:6.0f}s  EMERGENCY    {decision.explanation}")
                    if validation is not None:
                        print(f"                safety: {validation.status.value} "
                              f"({'; '.join(validation.reasons)})")
                    print(f"                applied to SUMO: {decision.applied}")

                # --- traffic resumed after the emergency ------------------ #
                if (decision.decision_type is DecisionType.TRAFFIC_OPTIMIZATION
                        and seen_emergency and not seen_traffic_resumed
                        and not orchestrator.get_city_state().emergency_vehicles):
                    seen_traffic_resumed = True
                    timeline.append({
                        "t": now,
                        "event": "traffic_resumed",
                        "explanation": decision.explanation,
                        "applied": decision.applied,
                    })
                    print(f"   t={now:6.0f}s  RESUMED      {decision.explanation}")

            if seen_emergency and not seen_emergency_cleared:
                if not orchestrator.get_city_state().emergency_vehicles:
                    seen_emergency_cleared = True
                    timeline.append({
                        "t": now, "event": "emergency_cleared",
                        "explanation": "ambulance has left the network; reservation released",
                    })
                    print(f"   t={now:6.0f}s  CLEARED      ambulance passed, emergency released")

        # ---------------------------------------------------------------- #
        # Final measured report
        # ---------------------------------------------------------------- #
        metrics = orchestrator.get_simulation_metrics()
        city = orchestrator.get_city_state()
        decisions = orchestrator.state.get_decisions()
        applied = [d for d in decisions if d.applied and d.signal_actions]
        rejected = [d for d in decisions if d.signal_actions and not d.applied]

        print()
        print("-- Measured results " + "-" * 55)
        print(f"   simulated time            : {metrics.simulation_time_s:.0f}s")
        print(f"   vehicles departed/arrived : {metrics.vehicles_departed} / {metrics.vehicles_arrived}")
        print(f"   average waiting time      : {metrics.average_waiting_time_s}s")
        print(f"   average queue length      : {metrics.average_queue_length_m} veh/edge")
        print(f"   average travel time       : {metrics.average_travel_time_s}s")
        print(f"   emergency travel time     : {metrics.emergency_vehicle_travel_time_s}s")
        print(f"   emergency response time   : {metrics.emergency_priority_response_time_s}s")
        print(f"   parking occupancy         : {metrics.parking_occupancy_pct}%")
        print(f"   accepted / rejected       : {metrics.accepted_decisions} / {metrics.rejected_decisions}")
        print(f"   processing latency        : {metrics.orchestrator_processing_latency_ms}ms")
        print(f"   agent availability        : {metrics.agent_availability_pct}%")

        print()
        print("-- Safety " + "-" * 66)
        print(f"   signal decisions applied  : {len(applied)}")
        print(f"   signal decisions rejected : {len(rejected)}")
        bad = [d for d in applied
               if d.validation is None or d.validation.status is not ValidationStatus.VALID]
        print(f"   applied without VALID     : {len(bad)}  (must be 0)")
        for decision in rejected[:5]:
            print(f"   rejected: {decision.rejected_reason}")

        print()
        print("-- Final city state " + "-" * 57)
        if city.traffic is not None:
            levels = city.traffic.junction_levels
            high = sorted(k for k, v in levels.items() if str(v).upper() == "HIGH")
            print(f"   congested junctions       : {high or 'none'}")
        else:
            print("   congested junctions       : traffic state unavailable")
        if city.parking is not None:
            print(f"   parking occupancy         : {city.parking.overall_occupancy_pct:.1f}%")
        print(f"   active emergencies        : {[e.vehicle_id for e in city.emergency_vehicles] or 'none'}")

        report = {
            "config": {
                "sumocfg": str(config.resolved_sumocfg()),
                "seed": config.seed,
                "end_time": config.end_time,
                "demand_scale": config.demand_scale,
            },
            "timeline": timeline,
            "metrics": metrics.model_dump(mode="json"),
            "agents": [s.model_dump(mode="json") for s in orchestrator.get_agent_statuses()],
            "safety": {
                "applied_signal_decisions": len(applied),
                "rejected_signal_decisions": len(rejected),
                "applied_without_valid": len(bad),
            },
            "events": orchestrator.events.stats(),
        }
    finally:
        orchestrator.shutdown()

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nTimeline written to {args.json_out}")

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
