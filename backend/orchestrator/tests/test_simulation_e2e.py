"""End-to-end test against the *real* SUMO scenario.

Unlike ``test_integration_mocked.py`` this drives the actual project
simulation (``simulation/orchestrator/unified.sumocfg``) through the real
:class:`SimulationCoordinator`, so it proves the orchestrator works against
live TraCI and the real agents' data paths.

The test is skipped (not failed) when SUMO or TraCI is unavailable, so the
suite stays runnable on a machine without a SUMO installation.

Every assertion is on **measured** state read back from the running
simulation. Nothing here asserts a fabricated success value: if SUMO reports
zero vehicles the test fails, it does not invent a number.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import shutil
from pathlib import Path

import pytest

from backend.orchestrator.config import OrchestratorConfig
from backend.orchestrator.orchestrator import TrafforaOrchestrator
from backend.orchestrator.schemas import AgentHealth, DecisionType, ValidationStatus

REPO_ROOT = Path(__file__).resolve().parents[3]
SUMOCFG = REPO_ROOT / "simulation" / "orchestrator" / "unified.sumocfg"

#: Short run: enough for the first vehicles to depart, queue and be measured,
#: while keeping the suite fast. The full demonstration uses ``--end 900``.
E2E_END_S = 200


def _sumo_available() -> bool:
    if shutil.which("sumo") is None:
        return False
    return importlib.util.find_spec("traci") is not None


pytestmark = pytest.mark.skipif(
    not _sumo_available() or not SUMOCFG.is_file(),
    reason="SUMO / TraCI or the unified scenario is not available",
)


@pytest.fixture(scope="module")
def config() -> OrchestratorConfig:
    base = OrchestratorConfig()
    return dataclasses.replace(
        base,
        sumocfg=SUMOCFG,
        end_time=E2E_END_S,
        label="orchestrator-e2e",
        seed=42,
        decision_interval_s=5.0,
        parking_interval_s=10.0,
    )


@pytest.fixture(scope="module")
def orchestrator(config: OrchestratorConfig):
    """One real SUMO session shared by every test in this module."""
    orch = TrafforaOrchestrator(config)
    orch.initialize()
    orch.start()
    try:
        # Advance to the end of the scenario once; the tests then inspect the
        # resulting state. ``run_step`` is the public control cycle.
        while orch.coordinator.current_time < E2E_END_S - 1:
            orch.run_step()
        yield orch
    finally:
        orch.shutdown()


def test_real_network_is_discovered(orchestrator: TrafforaOrchestrator) -> None:
    """The orchestrator sees the real signals and parking areas."""
    tls_ids = orchestrator.coordinator.known_tls_ids
    # The 5x5 grid ships 21 signalised junctions (A1..A3, B0..B4, C0..C4,
    # D0..D4, E1..E3). Assert the count we measured rather than a guess.
    assert len(tls_ids) == 21, f"expected 21 traffic lights, got {sorted(tls_ids)}"
    assert "A1" in tls_ids

    occupancy = orchestrator.coordinator.read_parking_occupancy()
    assert set(occupancy) == {"PA_A", "PA_B", "PA_C", "PA_D", "PA_E"}


def test_simulation_actually_ran(orchestrator: TrafforaOrchestrator) -> None:
    """SUMO advanced and real vehicles moved through the network."""
    metrics = orchestrator.get_simulation_metrics()
    assert metrics.simulation_time_s >= E2E_END_S - 2
    assert metrics.vehicles_departed > 0, "no vehicle departed: the scenario did not run"
    assert metrics.steps > 0


def test_metrics_are_measured_from_traci(orchestrator: TrafforaOrchestrator) -> None:
    """Waiting/queue/travel metrics come from the live simulation."""
    metrics = orchestrator.get_simulation_metrics()
    assert metrics.average_waiting_time_s is not None
    assert metrics.average_waiting_time_s >= 0.0
    assert metrics.average_queue_length_m is not None
    assert metrics.average_queue_length_m >= 0.0
    # Travel time is only defined once at least one trip completed.
    if metrics.vehicles_arrived > 0:
        assert metrics.average_travel_time_s is not None
        assert metrics.average_travel_time_s > 0.0


def test_agents_initialised_against_the_shared_session(
    orchestrator: TrafforaOrchestrator,
) -> None:
    """Each agent either came up READY or reports why it did not."""
    statuses = orchestrator.get_agent_statuses()
    assert len(statuses) == 3
    for status in statuses:
        assert status.health is not AgentHealth.UNKNOWN
        if status.health is not AgentHealth.READY:
            # A degraded agent must explain itself; silence would be a bug.
            assert status.message


def test_only_validated_actions_reached_sumo(orchestrator: TrafforaOrchestrator) -> None:
    """No decision was applied without a VALID safety result."""
    # Read the full history: ``get_decisions()`` is paginated for the API, and
    # a truncated view would make this count meaningless.
    decisions = orchestrator.state.get_decisions()
    applied = [d for d in decisions if d.applied and d.signal_actions]
    for decision in applied:
        assert decision.validation is not None, "applied decision has no validation record"
        assert decision.validation.status is ValidationStatus.VALID

    rejected = [d for d in decisions if d.rejected_reason]
    for decision in rejected:
        assert decision.applied is False

    # The coordinator counts its own writes; they must match what we applied.
    assert orchestrator.coordinator.applied_action_count == sum(
        len(d.signal_actions) for d in applied
    )


def test_city_state_is_dashboard_ready(orchestrator: TrafforaOrchestrator) -> None:
    """The city state serialises and carries live traffic + parking."""
    city = orchestrator.get_city_state()
    payload = city.model_dump(mode="json")
    assert payload["timestamp_s"] >= 0
    assert city.traffic is not None, "TrafficMind produced no traffic state"
    assert city.traffic.junction_levels, "traffic state has no per-junction levels"
    assert city.parking is not None, "ParkSense produced no parking state"
    assert city.parking.zones, "parking state has no zones"
    assert len(city.agents) == 3


def test_decisions_are_priority_ordered(orchestrator: TrafforaOrchestrator) -> None:
    """Every recorded decision carries a priority and an explanation."""
    for decision in orchestrator.get_decisions():
        assert 1 <= int(decision.priority) <= 3
        assert decision.explanation, f"decision {decision.decision_id} has no explanation"
        if decision.decision_type is DecisionType.PARKING_RECOMMENDATION:
            # Parking is advisory: it may never carry a signal write.
            assert decision.signal_actions == []
