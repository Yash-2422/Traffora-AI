"""Check whether following the signal recommendations helps, by simulating them.

THIS IS A TEST HARNESS, NOT PART OF THE AGENT. TrafficMind only *recommends*; in Phase 2 the
orchestrator will decide whether to apply a recommendation. To measure whether the advice is
any good, this script plays the orchestrator's part inside a simulation: it applies every
recommendation and compares the result with the same scenario under the normal fixed timing.

Green-time plans are applied at the start of each cycle (when a junction enters its first green)
and extend / end-early actions to the running green; yellow and all-red are never touched.

From the repository root::

    python -m backend.agents.traffic_mind.scripts.evaluate_signals --scenarios surge mixed --seeds 42 43
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import MonitorConfig, SimulationConfig, configure_logging
from ..congestion_detector import CongestionDetector
from ..exceptions import TrafficMindError
from ..schemas import SignalProgram
from ..signal_controller import SignalAdvisor, build_signal_program
from ..simulation_interface import SimulationInterface
from ..traffic_monitor import TrafficMonitor

logger = logging.getLogger("traffic_mind.evaluate_signals")

METRICS = (
    ("mean_waiting_s", "mean waiting time (s)", "lower"),
    ("mean_stopped", "mean stopped vehicles", "lower"),
    ("mean_vehicles", "mean vehicles in network", "lower"),
    ("high_share_pct", "junction time at HIGH (%)", "lower"),
    ("arrived", "vehicles that finished their trip", "higher"),
)


def _apply_plan(conn: Any, program: SignalProgram, greens: Dict[int, float]) -> None:
    """Install new green durations in the junction's program (clearance phases unchanged)."""
    jid = program.junction_id
    current_id = conn.trafficlight.getProgram(jid)
    logic = next(lg for lg in conn.trafficlight.getAllProgramLogics(jid) if lg.programID == current_id)
    for stage in program.stages:
        phase = logic.phases[stage.green_phase]
        phase.duration = phase.minDur = phase.maxDur = float(greens[stage.stage_id])
    conn.trafficlight.setProgramLogic(jid, logic)


def run_once(
    scenario: str, seed: int, until: int, adaptive: bool,
    audit: Optional[Dict[str, List[List[int]]]] = None,
) -> Dict[str, float]:
    """One run; returns the metrics.

    If ``audit`` is a dict it is filled with each junction's signal history as a list of
    ``[phase_index, seconds]`` runs, so tests can verify what the signals really did.
    """
    config = SimulationConfig(scenario=scenario, seed=seed, label=f"eval_{scenario}_{seed}_{int(adaptive)}")
    interval = MonitorConfig().sample_interval_s
    waits: List[float] = []
    stopped: List[float] = []
    vehicles: List[float] = []
    high = total = arrived = 0
    with SimulationInterface(config) as sim:
        conn = sim._require_running()   # the harness plays orchestrator, so it needs TraCI directly
        monitor = TrafficMonitor.from_source(sim)
        detector = CongestionDetector()
        advisor = SignalAdvisor.from_source(sim)
        programs = {jid: advisor.program(jid) for jid in advisor.junction_ids}
        previous_phase: Dict[str, Optional[int]] = {}
        while not sim.has_ended and sim.current_time < until:
            for _ in range(interval):
                sim.step(1)
                arrived += int(conn.simulation.getArrivedNumber())
                if audit is not None:
                    for jid in programs:
                        phase = int(conn.trafficlight.getPhase(jid))
                        runs = audit.setdefault(jid, [])
                        if runs and runs[-1][0] == phase:
                            runs[-1][1] += 1
                        else:
                            runs.append([phase, 1])
            snapshot = monitor.sample(sim)
            congestion = detector.update(snapshot)
            waits.append(snapshot.network.mean_waiting_time_s)
            stopped.append(snapshot.network.halting_vehicles)
            vehicles.append(snapshot.network.total_vehicles)
            high += sum(j.level.value == "HIGH" for j in congestion.junctions)
            total += len(congestion.junctions)
            if not adaptive:
                continue

            report = advisor.recommend(snapshot, congestion)
            for rec in report.recommendations:
                program = programs[rec.junction_id]
                first_green = program.stages[0].green_phase
                entered_cycle = (rec.current_phase == first_green
                                 and previous_phase.get(rec.junction_id) != first_green)
                previous_phase[rec.junction_id] = rec.current_phase
                remaining = next(j.phase_remaining_s for j in snapshot.junctions
                                 if j.junction_id == rec.junction_id)
                if entered_cycle:
                    plan = {s.stage_id: s.recommended_green_s for s in rec.stages}
                    if rec.action == "rebalance":
                        old_green = advisor.current_plan(rec.junction_id)[1]
                        elapsed = max(0.0, old_green - (remaining or 0.0))
                        _apply_plan(conn, program, plan)
                        advisor.set_current_plan(rec.junction_id, plan)
                        conn.trafficlight.setPhase(rec.junction_id, first_green)
                        conn.trafficlight.setPhaseDuration(rec.junction_id, max(1.0, plan[1] - elapsed))
                if rec.immediate.action == "extend_green":
                    conn.trafficlight.setPhaseDuration(rec.junction_id, (remaining or 0.0) + rec.immediate.extend_s)
                elif rec.immediate.action == "end_green_early":
                    conn.trafficlight.setPhaseDuration(rec.junction_id, 1.0)

    return {
        "mean_waiting_s": statistics.fmean(waits),
        "mean_stopped": statistics.fmean(stopped),
        "mean_vehicles": statistics.fmean(vehicles),
        "high_share_pct": 100.0 * high / max(1, total),
        "arrived": float(arrived),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare fixed timing with applied recommendations (test harness).")
    parser.add_argument("--scenarios", nargs="+", default=["surge", "mixed"])
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 43])
    parser.add_argument("--until", type=int, default=3000)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    results: Dict[str, Any] = {}
    try:
        for scenario in args.scenarios:
            fixed, adaptive = [], []
            for seed in args.seeds:
                fixed.append(run_once(scenario, seed, args.until, adaptive=False))
                adaptive.append(run_once(scenario, seed, args.until, adaptive=True))
                print(f"  {scenario} seed {seed} done")
            results[scenario] = {"fixed": fixed, "adaptive": adaptive}
            print(f"\n{scenario} (average of {len(args.seeds)} run(s), first {args.until} s)")
            print(f"{'metric':<36}{'fixed timing':>13}{'recommended':>13}{'change':>9}")
            for key, label, better in METRICS:
                a = statistics.fmean(r[key] for r in fixed)
                b = statistics.fmean(r[key] for r in adaptive)
                change = (b - a) / a * 100 if a else 0.0
                print(f"{label:<36}{a:>13.2f}{b:>13.2f}{change:>+8.1f}%")
            print()
    except TrafficMindError as exc:
        logger.error("%s: %s", exc.code, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 130
    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"Results written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())