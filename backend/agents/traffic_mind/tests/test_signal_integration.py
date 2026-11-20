"""Signal recommendations on a real SUMO run (skipped without SUMO).

The audit test plays the orchestrator (see scripts/evaluate_signals.py), applies every
recommendation for 25 simulated minutes of the surge scenario, and checks from the signals'
actual phase history that the safety rules held in the simulation, not just on paper.
"""

import pytest

from backend.agents.traffic_mind import simulation_interface as si
from backend.agents.traffic_mind.config import SignalConfig, SimulationConfig
from backend.agents.traffic_mind.scripts.evaluate_signals import run_once
from backend.agents.traffic_mind.signal_controller import SignalAdvisor, validate_plan
from backend.agents.traffic_mind.simulation_interface import SimulationInterface
from backend.agents.traffic_mind.traffic_monitor import TrafficMonitor

SCENARIO = SimulationConfig(scenario="surge", seed=42)
READY = si.find_sumo_binary() is not None and SCENARIO.resolved_sumocfg().is_file()
needs_sumo = pytest.mark.skipif(not READY, reason="SUMO or the surge scenario is missing")
CFG = SignalConfig()


@needs_sumo
def test_programs_are_read_from_the_network():
    with SimulationInterface(SCENARIO) as sim:
        advisor = SignalAdvisor.from_source(sim)
        assert advisor.junction_ids == ["A0", "A1", "A2", "B0", "B1", "B2", "C0", "C1", "C2"]
        program = advisor.program("B1")
        assert [s.edges for s in program.stages] == [["B0B1", "B2B1"], ["A1B1", "C1B1"]]
        assert program.cycle_s == 70 and advisor.current_plan("B1") == {1: 30, 2: 30}
        validate_plan(program, advisor.current_plan("B1"), CFG)


@needs_sumo
def test_live_recommendations_are_validated():
    with SimulationInterface(SCENARIO) as sim:
        monitor, advisor = TrafficMonitor.from_source(sim), SignalAdvisor.from_source(sim)
        report = None
        while sim.current_time < 1500:
            sim.step(5)
            report = advisor.recommend(monitor.sample(sim))
    assert len(report.recommendations) == 9 and report.mode == "recommend_only"
    assert all(r.safety.validated for r in report.recommendations)
    assert any(r.action == "rebalance" for r in report.recommendations)     # the surge builds up by 1500 s


@needs_sumo
def test_applied_recommendations_never_break_a_safety_rule_in_the_simulation():
    audit = {}
    run_once("surge", 42, 1500, adaptive=True, audit=audit)
    assert len(audit) == 9
    changed = 0
    for jid, runs in audit.items():
        # the phase order is never changed: 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 0 ...
        for (a, _), (b, _) in zip(runs, runs[1:]):
            assert b == (a + 1) % 6, f"{jid}: phase {a} was followed by {b}"
        inner = runs[1:-1]                                       # the first and last runs are cut off
        for phase, seconds in inner:
            if phase in (1, 4):
                assert seconds == 3, f"{jid}: yellow lasted {seconds} s"
            elif phase in (2, 5):
                assert seconds == 2, f"{jid}: all-red lasted {seconds} s"
            else:
                assert CFG.min_green_s <= seconds <= CFG.max_green_s, f"{jid}: green lasted {seconds} s"
                changed += seconds != 30
    assert changed > 0, "the test would be meaningless if no green was ever changed"