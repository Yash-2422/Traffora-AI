"""Integration tests for the main orchestrator, with mocked SUMO and agents.

Everything here runs through the real control cycle -- collect, publish,
drain, decide, validate, apply -- but against ``FakeCoordinator`` so the
outcome is deterministic and no SUMO binary is required.
"""

from __future__ import annotations

import dataclasses

import pytest

from backend.orchestrator.agent_registry import AgentAdapter, AgentRegistry, AgentSnapshot
from backend.orchestrator.config import OrchestratorConfig
from backend.orchestrator.decision_engine import DecisionEngine
from backend.orchestrator.event_manager import EventManager
from backend.orchestrator.exceptions import OrchestratorError
from backend.orchestrator.orchestrator import TrafforaOrchestrator
from backend.orchestrator.safety_validator import SafetyValidator
from backend.orchestrator.schemas import (
    AgentHealth,
    AgentName,
    DecisionType,
    EventPriority,
    ParkingRecommendation,
    TrafficState,
)
from backend.orchestrator.shared_state import SharedState

from .conftest import (
    FakeCoordinator,
    make_emergency,
    make_parking,
    make_recommendation,
    make_signal_action,
)


class ScriptedAdapter(AgentAdapter):
    """Adapter whose snapshot is produced by a test-supplied function."""

    def __init__(self, name: AgentName, factory=None, fail_init: bool = False) -> None:
        super().__init__()
        self.name = name
        self._factory = factory
        self._fail_init = fail_init

    def initialize(self, coordinator) -> None:
        if self._fail_init:
            self._mark_failed("scripted init failure")
            raise RuntimeError("scripted init failure")
        self._mark_ready("scripted ready")

    def collect(self, coordinator, timestamp_s: float) -> AgentSnapshot:
        if self._factory is None:
            return AgentSnapshot(agent=self.name)
        return self._factory(timestamp_s)


def _config(**overrides) -> OrchestratorConfig:
    base = OrchestratorConfig(decision_interval_s=1.0, parking_interval_s=1.0)
    return dataclasses.replace(base, **overrides) if overrides else base


def _build(adapters, config=None, coordinator=None) -> TrafforaOrchestrator:
    registry = AgentRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return TrafforaOrchestrator(
        config or _config(),
        coordinator=coordinator or FakeCoordinator(),
        registry=registry,
        event_manager=EventManager(),
        decision_engine=DecisionEngine(),
        safety_validator=SafetyValidator(),
        state=SharedState(),
    )


def _emergency_factory(vehicle_id="amb_1", tls_id="A1", duration=25.0):
    def factory(timestamp_s: float) -> AgentSnapshot:
        action = make_signal_action(tls_id, duration, emergency_vehicle_id=vehicle_id,
                                    source_agent=AgentName.GREEN_CORRIDOR,
                                    created_at_s=timestamp_s)
        rec = make_recommendation(
            AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
            EventPriority.EMERGENCY, signal_actions=[action],
            explanation="ambulance approaching")
        return AgentSnapshot(agent=AgentName.GREEN_CORRIDOR,
                             emergencies=[make_emergency(vehicle_id, tls_id)],
                             recommendations=[rec])
    return factory


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_initialize_starts_simulation_and_agents():
    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)], coordinator=coordinator)

    orch.initialize()

    assert coordinator.is_running
    assert orch.registry.get(AgentName.TRAFFIC_MIND).status().health is AgentHealth.READY
    assert orch.validator.known_tls == {"A1", "B1", "C1"}


def test_run_step_before_initialize_raises():
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)])
    with pytest.raises(OrchestratorError):
        orch.run_step()


def test_run_step_before_start_raises():
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)])
    orch.initialize()
    with pytest.raises(OrchestratorError):
        orch.run_step()


def test_initialize_is_idempotent():
    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)], coordinator=coordinator)
    orch.initialize()
    orch.initialize()
    assert coordinator.steps == 0


# --------------------------------------------------------------------------- #
# Control cycle
# --------------------------------------------------------------------------- #
def test_run_step_without_recommendations_records_no_action():
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)])
    orch.start()

    decisions = orch.run_step()

    assert len(decisions) == 1
    assert decisions[0].decision_type is DecisionType.NO_ACTION
    assert orch.state.snapshot().latest_decision is not None


def test_emergency_action_is_validated_and_applied():
    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.GREEN_CORRIDOR,
                                   _emergency_factory(duration=25.0))],
                  coordinator=coordinator)
    orch.start()

    decisions = orch.run_step()

    assert decisions[0].decision_type is DecisionType.EMERGENCY_PRIORITY
    assert decisions[0].applied is True
    assert coordinator.connection.writes == [("A1", 25.0)]
    assert coordinator.applied[0].tls_id == "A1"


def test_invalid_signal_action_is_never_sent_to_sumo():
    """A phase change must be rejected before it reaches the connection."""
    def factory(timestamp_s: float) -> AgentSnapshot:
        bad = make_signal_action("A1", 25.0, phase_index=1,
                                 emergency_vehicle_id="amb_1")
        rec = make_recommendation(AgentName.GREEN_CORRIDOR,
                                  DecisionType.EMERGENCY_PRIORITY,
                                  EventPriority.EMERGENCY, signal_actions=[bad])
        return AgentSnapshot(agent=AgentName.GREEN_CORRIDOR,
                             emergencies=[make_emergency()], recommendations=[rec])

    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.GREEN_CORRIDOR, factory)],
                  coordinator=coordinator)
    orch.start()

    decisions = orch.run_step()

    assert decisions[0].applied is False
    assert "phase change" in (decisions[0].rejected_reason or "")
    assert coordinator.connection.writes == []


def test_baseline_mode_never_writes_to_sumo():
    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.GREEN_CORRIDOR, _emergency_factory())],
                  config=_config(enable_signal_control=False), coordinator=coordinator)
    orch.start()

    decisions = orch.run_step()

    assert decisions[0].applied is False
    assert "baseline" in (decisions[0].rejected_reason or "")
    assert coordinator.connection.writes == []


def test_traffic_recommendation_is_applied_on_congested_light():
    def factory(timestamp_s: float) -> AgentSnapshot:
        action = make_signal_action("A1", 30.0, source_agent=AgentName.TRAFFIC_MIND,
                                    created_at_s=timestamp_s)
        rec = make_recommendation(AgentName.TRAFFIC_MIND,
                                  DecisionType.TRAFFIC_OPTIMIZATION,
                                  EventPriority.TRAFFIC, signal_actions=[action])
        traffic = TrafficState(timestamp_s=timestamp_s, vehicle_count=80,
                               average_waiting_time_s=25.0, average_queue_length_m=90.0,
                               congestion_level="HIGH", junction_levels={"A1": "HIGH"})
        return AgentSnapshot(agent=AgentName.TRAFFIC_MIND, traffic=traffic,
                             recommendations=[rec])

    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND, factory)],
                  coordinator=coordinator)
    orch.start()

    decisions = orch.run_step()

    assert decisions[0].decision_type is DecisionType.TRAFFIC_OPTIMIZATION
    assert coordinator.connection.writes == [("A1", 30.0)]


def test_parking_recommendation_is_recorded_but_never_written():
    def factory(timestamp_s: float) -> AgentSnapshot:
        rec = make_recommendation(
            AgentName.PARK_SENSE, DecisionType.PARKING_RECOMMENDATION,
            EventPriority.PARKING,
            parking_recommendations=[ParkingRecommendation(zone_id="PA_B", available=7,
                                                           occupancy_pct=41.0, score=0.8)])
        return AgentSnapshot(agent=AgentName.PARK_SENSE, parking=make_parking(90.0),
                             recommendations=[rec])

    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.PARK_SENSE, factory)], coordinator=coordinator)
    orch.start()

    decisions = orch.run_step()

    assert decisions[0].decision_type is DecisionType.PARKING_RECOMMENDATION
    assert decisions[0].signal_actions == []
    assert coordinator.connection.writes == []
    assert orch.state.snapshot().parking is not None


def test_emergency_priority_wins_over_traffic_in_a_full_cycle():
    def emergency_factory(timestamp_s: float) -> AgentSnapshot:
        action = make_signal_action("A1", 25.0, emergency_vehicle_id="amb_1",
                                    created_at_s=timestamp_s)
        rec = make_recommendation(AgentName.GREEN_CORRIDOR,
                                  DecisionType.EMERGENCY_PRIORITY,
                                  EventPriority.EMERGENCY, signal_actions=[action])
        return AgentSnapshot(agent=AgentName.GREEN_CORRIDOR,
                             emergencies=[make_emergency()], recommendations=[rec])

    def traffic_factory(timestamp_s: float) -> AgentSnapshot:
        action = make_signal_action("A1", 30.0, source_agent=AgentName.TRAFFIC_MIND,
                                    created_at_s=timestamp_s)
        rec = make_recommendation(AgentName.TRAFFIC_MIND,
                                  DecisionType.TRAFFIC_OPTIMIZATION,
                                  EventPriority.TRAFFIC, signal_actions=[action])
        traffic = TrafficState(timestamp_s=timestamp_s, vehicle_count=80,
                               average_waiting_time_s=25.0, average_queue_length_m=90.0,
                               congestion_level="HIGH", junction_levels={"A1": "HIGH"})
        return AgentSnapshot(agent=AgentName.TRAFFIC_MIND, traffic=traffic,
                             recommendations=[rec])

    coordinator = FakeCoordinator()
    orch = _build([
        ScriptedAdapter(AgentName.GREEN_CORRIDOR, emergency_factory),
        ScriptedAdapter(AgentName.TRAFFIC_MIND, traffic_factory),
    ], coordinator=coordinator)
    orch.start()

    decisions = orch.run_step()

    assert decisions[0].decision_type is DecisionType.EMERGENCY_PRIORITY
    assert coordinator.connection.writes == [("A1", 25.0)]


# --------------------------------------------------------------------------- #
# Failure isolation and shutdown
# --------------------------------------------------------------------------- #
def test_failed_agent_does_not_stop_the_cycle():
    orch = _build([
        ScriptedAdapter(AgentName.TRAFFIC_MIND, fail_init=True),
        ScriptedAdapter(AgentName.PARK_SENSE),
    ])
    orch.start()

    decisions = orch.run_step()

    assert decisions[0].decision_type is DecisionType.NO_ACTION
    assert orch.registry.get(AgentName.TRAFFIC_MIND).status().health is AgentHealth.FAILED


def test_shutdown_closes_the_single_simulation_session():
    coordinator = FakeCoordinator()
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)], coordinator=coordinator)
    orch.start()
    orch.run_step()

    orch.shutdown()

    assert coordinator.is_running is False
    assert orch.is_running is False
    assert orch.registry.get(AgentName.TRAFFIC_MIND).status().health is AgentHealth.STOPPED


def test_context_manager_starts_and_stops():
    coordinator = FakeCoordinator()
    with _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)], coordinator=coordinator) as orch:
        assert orch.is_running
        orch.run_step()
    assert coordinator.is_running is False


# --------------------------------------------------------------------------- #
# Read-only service surface
# --------------------------------------------------------------------------- #
def test_service_methods_return_serialisable_payloads():
    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)])
    orch.start()
    orch.run_step()

    city = orch.get_city_state()
    status = orch.get_system_status()
    metrics = orch.get_simulation_metrics()

    # FastAPI serialises pydantic models directly; the values must still be
    # JSON-safe and reflect the step that just ran.
    assert city.model_dump(mode="json")["timestamp_s"] >= 0
    assert status.agents
    assert metrics.model_dump(mode="json")["simulation_time_s"] >= 0
    assert isinstance(orch.get_agent_statuses(), list)
    assert orch.get_latest_decision() is not None
    assert orch.get_decisions()
    assert orch.get_emergency_events() == []
    assert "parking" in orch.get_parking_analytics()


def test_duplicate_events_are_suppressed_across_cycles():
    """Two identical agent events must produce exactly one queue entry."""
    from backend.orchestrator.schemas import AgentName as AN, EventType

    orch = _build([ScriptedAdapter(AgentName.TRAFFIC_MIND)])
    orch.start()
    orch.run_step()

    first = orch.events.create_event(EventType.CONGESTION_DETECTED, AN.TRAFFIC_MIND,
                                     timestamp_s=1.0, payload={"junction_id": "A1"})
    second = orch.events.create_event(EventType.CONGESTION_DETECTED, AN.TRAFFIC_MIND,
                                      timestamp_s=2.0, payload={"junction_id": "A1"})

    assert orch.events.try_publish(first) is True
    assert orch.events.try_publish(second) is False
    assert orch.events.duplicate_count == 1
    assert orch.events.pending_count == 1
