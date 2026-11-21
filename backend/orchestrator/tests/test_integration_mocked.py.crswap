"""End-to-end scenario test for the whole orchestrator, with mocked SUMO.

This exercises the complete demonstration timeline described in the project
brief, through the *real* control cycle (collect -> publish -> drain -> decide
-> validate -> apply -> record) and against ``FakeCoordinator``:

    normal traffic
      -> TrafficMind detects congestion at A1
      -> ParkSense reports high occupancy and recommends an alternative zone
      -> an ambulance enters the network
      -> GreenCorridor requests signal priority at A1
      -> the orchestrator prioritises the emergency over the traffic request
      -> the safety validator checks the transition
      -> the coordinator applies the (only) safe write to the signal
      -> the ambulance passes, the emergency is cleared
      -> traffic optimisation resumes

Nothing here claims to be a SUMO measurement: the coordinator is a fake, so
the assertions are about *orchestration behaviour* (which decision wins, what
is written, what is suppressed). The real-SUMO counterpart lives in
``test_simulation_e2e.py`` and is skipped when SUMO is unavailable.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List

import pytest

from backend.orchestrator.agent_registry import AgentAdapter, AgentRegistry, AgentSnapshot
from backend.orchestrator.config import DecisionSettings, OrchestratorConfig
from backend.orchestrator.decision_engine import DecisionEngine
from backend.orchestrator.event_manager import EventManager
from backend.orchestrator.orchestrator import TrafforaOrchestrator
from backend.orchestrator.safety_validator import SafetyValidator
from backend.orchestrator.schemas import (
    AgentName,
    DecisionType,
    EmergencyEvent,
    Event,
    EventPriority,
    EventType,
    ParkingRecommendation,
    ParkingState,
    ParkingZoneState,
    TrafficState,
)
from backend.orchestrator.shared_state import SharedState

from .conftest import FakeCoordinator, make_recommendation, make_signal_action

# Timeline (simulated seconds). Each ``run_step`` advances SUMO by one second.
CONGESTION_START = 9
CONGESTION_END = 16
EMERGENCY_START = 17
EMERGENCY_END = 24
RECOVERY_START = 25
TOTAL_STEPS = 32


# --------------------------------------------------------------------------- #
# Scenario adapters
# --------------------------------------------------------------------------- #
class ScenarioAdapter(AgentAdapter):
    """Adapter whose snapshot depends on the simulated clock."""

    def __init__(self, name: AgentName, factory) -> None:
        super().__init__()
        self.name = name
        self._factory = factory

    def initialize(self, coordinator) -> None:
        self._mark_ready("scenario ready")

    def collect(self, coordinator, timestamp_s: float) -> AgentSnapshot:
        return self._factory(timestamp_s)


def _traffic_state(level_a1: str, timestamp_s: float) -> TrafficState:
    return TrafficState(
        timestamp_s=timestamp_s,
        vehicle_count=120,
        average_waiting_time_s=4.0 if level_a1 == "HIGH" else 1.0,
        average_queue_length_m=45.0 if level_a1 == "HIGH" else 5.0,
        congestion_level=level_a1,
        junction_levels={"A1": level_a1, "B1": "LOW"},
    )


def _traffic_factory(timestamp_s: float) -> AgentSnapshot:
    congested = CONGESTION_START <= timestamp_s <= CONGESTION_END or timestamp_s >= RECOVERY_START
    if not congested:
        return AgentSnapshot(
            agent=AgentName.TRAFFIC_MIND,
            traffic=_traffic_state("LOW", timestamp_s),
        )

    action = make_signal_action(
        "A1", 30.0, source_agent=AgentName.TRAFFIC_MIND, created_at_s=timestamp_s
    )
    recommendation = make_recommendation(
        AgentName.TRAFFIC_MIND,
        DecisionType.TRAFFIC_OPTIMIZATION,
        EventPriority.TRAFFIC,
        signal_actions=[action],
        explanation="A1 queue is HIGH; extend the running green.",
    )
    event = Event(
        event_id=f"cong-{timestamp_s:.0f}",
        event_type=EventType.CONGESTION_DETECTED,
        source_agent=AgentName.TRAFFIC_MIND,
        timestamp_s=timestamp_s,
        priority=EventPriority.TRAFFIC,
        payload={"junction_id": "A1", "level": "HIGH", "queue_length_m": 45.0},
        dedupe_key="congestion|A1",
    )
    return AgentSnapshot(
        agent=AgentName.TRAFFIC_MIND,
        traffic=_traffic_state("HIGH", timestamp_s),
        recommendations=[recommendation],
        events=[event],
    )


def _parking_state(timestamp_s: float) -> ParkingState:
    zones = [
        ParkingZoneState(zone_id="PA_A", capacity=10, occupied=9, available=1, occupancy_pct=90.0),
        ParkingZoneState(zone_id="PA_B", capacity=12, occupied=4, available=8, occupancy_pct=33.3),
    ]
    return ParkingState(
        timestamp_s=timestamp_s,
        zones=zones,
        total_capacity=22,
        total_occupied=13,
        total_available=9,
        overall_occupancy_pct=59.1,
    )


def _parking_factory(timestamp_s: float) -> AgentSnapshot:
    recommendation = make_recommendation(
        AgentName.PARK_SENSE,
        DecisionType.PARKING_RECOMMENDATION,
        EventPriority.PARKING,
        parking_recommendations=[
            ParkingRecommendation(zone_id="PA_B", name="North Garage", score=0.91,
                                  available=8, occupancy_pct=33.3,
                                  reason="8 free spaces, low approach delay"),
            ParkingRecommendation(zone_id="PA_A", name="Central Lot", score=0.20,
                                  available=1, occupancy_pct=90.0,
                                  reason="almost full"),
        ],
        explanation="PA_A is 90% occupied; PA_B has capacity.",
    )
    event = Event(
        event_id=f"park-{timestamp_s:.0f}",
        event_type=EventType.PARKING_DEMAND_ALERT,
        source_agent=AgentName.PARK_SENSE,
        timestamp_s=timestamp_s,
        priority=EventPriority.PARKING,
        payload={"zone_id": "PA_A", "level": "HIGH", "occupancy_pct": 90.0},
        dedupe_key="parking|PA_A|HIGH",
    )
    return AgentSnapshot(
        agent=AgentName.PARK_SENSE,
        parking=_parking_state(timestamp_s),
        recommendations=[recommendation],
        events=[event],
    )


def _green_corridor_factory(timestamp_s: float) -> AgentSnapshot:
    if EMERGENCY_START <= timestamp_s <= EMERGENCY_END:
        emergency = EmergencyEvent(
            event_id="emg-amb_1",
            vehicle_id="amb_1",
            timestamp_s=timestamp_s,
            tls_id="A1",
            distance_m=80.0,
            eta_s=max(1.0, 25.0 - timestamp_s),
            route=["A0A1", "A1B1"],
        )
        action = make_signal_action(
            "A1", 25.0,
            source_agent=AgentName.GREEN_CORRIDOR,
            created_at_s=timestamp_s,
            emergency_vehicle_id="amb_1",
        )
        recommendation = make_recommendation(
            AgentName.GREEN_CORRIDOR,
            DecisionType.EMERGENCY_PRIORITY,
            EventPriority.EMERGENCY,
            signal_actions=[action],
            explanation="ambulance amb_1 approaching A1",
        )
        event = Event(
            event_id="emg-detected-amb_1",
            event_type=EventType.EMERGENCY_DETECTED,
            source_agent=AgentName.GREEN_CORRIDOR,
            timestamp_s=timestamp_s,
            priority=EventPriority.EMERGENCY,
            payload={"vehicle_id": "amb_1", "tls_id": "A1", "eta_s": emergency.eta_s},
            dedupe_key="emergency|amb_1",
        )
        return AgentSnapshot(
            agent=AgentName.GREEN_CORRIDOR,
            emergencies=[emergency],
            recommendations=[recommendation],
            events=[event],
        )

    if timestamp_s > EMERGENCY_END:
        # The ambulance has reached the hospital: report the arrival so the
        # orchestrator can release the reservation.
        return AgentSnapshot(
            agent=AgentName.GREEN_CORRIDOR,
            arrived_emergencies=["amb_1"],
        )

    return AgentSnapshot(agent=AgentName.GREEN_CORRIDOR)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _config() -> OrchestratorConfig:
    base = OrchestratorConfig(decision_interval_s=1.0, parking_interval_s=1.0)
    # Short cooldowns keep the scenario readable: one write per window rather
    # than a write on every simulated second.
    decisions = DecisionSettings(signal_action_cooldown_s=10.0, traffic_cooldown_s=10.0)
    return dataclasses.replace(base, decisions=decisions)


def _build() -> TrafforaOrchestrator:
    config = _config()
    registry = AgentRegistry()
    registry.register(ScenarioAdapter(AgentName.TRAFFIC_MIND, _traffic_factory))
    registry.register(ScenarioAdapter(AgentName.GREEN_CORRIDOR, _green_corridor_factory))
    registry.register(ScenarioAdapter(AgentName.PARK_SENSE, _parking_factory))

    return TrafforaOrchestrator(
        config,
        coordinator=FakeCoordinator(tls_ids=("A1", "B1")),
        registry=registry,
        event_manager=EventManager(),
        decision_engine=DecisionEngine(config.decisions),
        safety_validator=SafetyValidator(config.safety),
        state=SharedState(),
    )


@pytest.fixture
def scenario() -> TrafforaOrchestrator:
    orchestrator = _build()
    orchestrator.initialize()
    orchestrator.start()
    yield orchestrator
    orchestrator.shutdown()


def _run(orchestrator: TrafforaOrchestrator, steps: int) -> List:
    decisions = []
    for _ in range(steps):
        decisions.extend(orchestrator.run_step())
    return decisions


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_full_scenario_priorities_and_writes(scenario: TrafforaOrchestrator) -> None:
    """Emergency wins over traffic, parking never writes, and traffic resumes."""
    coordinator: FakeCoordinator = scenario.coordinator

    # -- phase 1: normal traffic ---------------------------------------- #
    normal = _run(scenario, CONGESTION_START - 1)
    # ParkSense publishes an advisory every cycle, so "no decision at all" is
    # the wrong expectation. What matters is that nothing *acts*: no traffic or
    # emergency decision is produced and no signal is written.
    acting = [d for d in normal
              if d.decision_type in (DecisionType.TRAFFIC_OPTIMIZATION,
                                     DecisionType.EMERGENCY_PRIORITY)]
    assert acting == []
    assert coordinator.connection.writes == []

    # -- phase 2: congestion + high parking occupancy -------------------- #
    congestion = _run(scenario, CONGESTION_END - CONGESTION_START + 1)
    traffic = [d for d in congestion if d.decision_type is DecisionType.TRAFFIC_OPTIMIZATION]
    parking = [d for d in congestion if d.decision_type is DecisionType.PARKING_RECOMMENDATION]
    assert traffic, "TrafficMind congestion should produce a traffic decision"
    assert traffic[0].applied is True
    assert [a.tls_id for a in traffic[0].signal_actions] == ["A1"]
    # Parking is advisory: it is recorded but never touches a signal.
    assert parking, "ParkSense should produce a parking recommendation"
    assert parking[0].signal_actions == []
    assert parking[0].applied is True
    assert parking[0].parking_recommendations[0].zone_id == "PA_B"
    # Exactly one write so far: the A1 green extension from TrafficMind.
    assert coordinator.connection.writes == [("A1", 30.0)]

    # -- phase 3: ambulance enters, emergency preempts traffic ----------- #
    writes_before = len(coordinator.connection.writes)
    emergency = _run(scenario, EMERGENCY_END - EMERGENCY_START + 1)
    emergency_decisions = [
        d for d in emergency if d.decision_type is DecisionType.EMERGENCY_PRIORITY
    ]
    assert emergency_decisions, "GreenCorridor should request emergency priority"
    first = emergency_decisions[0]
    assert first.priority is EventPriority.EMERGENCY
    assert first.applied is True
    assert first.signal_actions[0].emergency_vehicle_id == "amb_1"
    # The traffic action for the same light must be suppressed, not applied.
    suppressed_traffic = [
        d for d in emergency
        if d.decision_type is DecisionType.TRAFFIC_OPTIMIZATION and not d.applied
    ]
    assert not suppressed_traffic, "suppressed traffic actions should not be emitted at all"
    # Only the emergency write reached SUMO during this window.
    new_writes = coordinator.connection.writes[writes_before:]
    assert new_writes == [("A1", 25.0)]

    # -- phase 4: ambulance has passed, traffic optimisation resumes ----- #
    recovery = _run(scenario, TOTAL_STEPS - EMERGENCY_END)
    resumed = [d for d in recovery if d.decision_type is DecisionType.TRAFFIC_OPTIMIZATION]
    assert resumed, "traffic optimisation should resume once the emergency clears"
    assert resumed[0].applied is True
    assert coordinator.connection.writes[-1] == ("A1", 30.0)

    # The emergency must be gone from shared state.
    city = scenario.get_city_state()
    assert city.emergency_vehicles == []


def test_scenario_metrics_come_from_the_coordinator(scenario: TrafforaOrchestrator) -> None:
    """Metrics are passed through from the coordinator, never invented."""
    _run(scenario, TOTAL_STEPS)
    metrics = scenario.get_simulation_metrics()

    # FakeCoordinator reports a constant 2.5 s average wait; the orchestrator
    # must surface exactly that, not a plausible-looking guess.
    assert metrics.average_waiting_time_s == pytest.approx(2.5)
    assert metrics.average_travel_time_s == pytest.approx(100.0)
    assert metrics.vehicles_departed > 0
    assert metrics.agent_availability_pct == 100.0
    assert metrics.accepted_decisions > 0
    assert metrics.orchestrator_processing_latency_ms >= 0.0
    # The emergency response time is measured from first-seen to first-write.
    assert metrics.emergency_priority_response_time_s is not None


def test_scenario_emergency_state_is_cleared_after_pass(
    scenario: TrafforaOrchestrator,
) -> None:
    """The emergency reservation is released when GreenCorridor reports arrival."""
    _run(scenario, EMERGENCY_END)
    during = scenario.get_city_state()
    assert [e.vehicle_id for e in during.emergency_vehicles] == ["amb_1"]

    _run(scenario, TOTAL_STEPS - EMERGENCY_END)
    after = scenario.get_city_state()
    assert after.emergency_vehicles == []


def test_scenario_decisions_are_ordered_by_priority(scenario: TrafforaOrchestrator) -> None:
    """In any single cycle emergency decisions come before traffic and parking."""
    for _ in range(TOTAL_STEPS):
        decisions = scenario.run_step()
        priorities = [int(d.priority) for d in decisions if d.decision_type is not DecisionType.NO_ACTION]
        assert priorities == sorted(priorities), f"decisions out of priority order: {decisions}"
