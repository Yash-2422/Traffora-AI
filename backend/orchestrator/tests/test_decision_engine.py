"""Unit tests for the deterministic decision engine.

These tests pin the priority contract that the whole system depends on:

* P1 emergency always wins and suppresses traffic actions on the same light,
* P2 traffic only acts on genuinely congested junctions and is rate limited,
* P3 parking is advisory and can never emit a signal action,
* identical inputs produce identical decisions (no LLM, no randomness).
"""

from __future__ import annotations

import pytest

from backend.orchestrator.config import DecisionSettings
from backend.orchestrator.decision_engine import DecisionEngine
from backend.orchestrator.schemas import (
    AgentName,
    CityState,
    DecisionType,
    EventPriority,
    ParkingRecommendation,
    TrafficState,
)

from .conftest import make_emergency, make_recommendation, make_signal_action


def _city(traffic=None, emergencies=None, parking=None, now: float = 10.0) -> CityState:
    return CityState(
        timestamp_s=now,
        traffic=traffic,
        emergency_vehicles=emergencies or [],
        parking=parking,
    )


def _traffic_state(levels, now: float = 10.0) -> TrafficState:
    return TrafficState(
        timestamp_s=now,
        vehicle_count=120,
        average_waiting_time_s=20.0,
        average_queue_length_m=80.0,
        congestion_level="HIGH",
        junction_levels=levels,
    )


@pytest.fixture
def engine() -> DecisionEngine:
    return DecisionEngine(DecisionSettings())


# --------------------------------------------------------------------------- #
# Empty input
# --------------------------------------------------------------------------- #
def test_no_recommendations_yields_a_single_no_action(engine):
    decisions = engine.decide(_city(), [])
    assert len(decisions) == 1
    assert decisions[0].decision_type is DecisionType.NO_ACTION
    assert decisions[0].signal_actions == []


# --------------------------------------------------------------------------- #
# Priority 1: emergency
# --------------------------------------------------------------------------- #
def test_emergency_recommendation_becomes_a_p1_decision(engine):
    action = make_signal_action("A1", 25.0, emergency_vehicle_id="amb_1",
                                source_agent=AgentName.GREEN_CORRIDOR)
    rec = make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                              EventPriority.EMERGENCY, signal_actions=[action])

    decisions = engine.decide(_city(emergencies=[make_emergency()]), [rec])

    assert len(decisions) == 1
    assert decisions[0].decision_type is DecisionType.EMERGENCY_PRIORITY
    assert decisions[0].priority is EventPriority.EMERGENCY
    assert decisions[0].signal_actions[0].tls_id == "A1"
    assert "Emergency priority (P1)" in decisions[0].explanation


def test_emergency_suppresses_traffic_action_on_the_same_light(engine):
    emergency_action = make_signal_action("A1", 25.0, emergency_vehicle_id="amb_1")
    traffic_action = make_signal_action("A1", 30.0, source_agent=AgentName.TRAFFIC_MIND)
    other_traffic = make_signal_action("B1", 30.0, source_agent=AgentName.TRAFFIC_MIND)

    recs = [
        make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                            EventPriority.EMERGENCY, signal_actions=[emergency_action]),
        make_recommendation(AgentName.TRAFFIC_MIND, DecisionType.TRAFFIC_OPTIMIZATION,
                            EventPriority.TRAFFIC,
                            signal_actions=[traffic_action, other_traffic]),
    ]
    city = _city(traffic=_traffic_state({"A1": "HIGH", "B1": "HIGH"}),
                 emergencies=[make_emergency()])

    decisions = engine.decide(city, recs)

    by_type = {d.decision_type: d for d in decisions}
    assert by_type[DecisionType.EMERGENCY_PRIORITY].signal_actions[0].tls_id == "A1"
    traffic = by_type[DecisionType.TRAFFIC_OPTIMIZATION]
    assert [a.tls_id for a in traffic.signal_actions] == ["B1"]
    assert traffic.metadata["suppressed_tls"] == ["A1"]


def test_emergency_preemption_is_not_reissued_every_step(engine):
    action = make_signal_action("A1", 25.0, emergency_vehicle_id="amb_1")
    rec = make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                              EventPriority.EMERGENCY, signal_actions=[action])
    city = _city(emergencies=[make_emergency()])

    first = engine.decide(city, [rec], timestamp_s=10.0)
    second = engine.decide(city, [rec], timestamp_s=11.0)

    assert first[0].decision_type is DecisionType.EMERGENCY_PRIORITY
    assert second[0].decision_type is DecisionType.NO_ACTION


def test_emergency_preemption_is_reissued_after_the_cooldown(engine):
    action = make_signal_action("A1", 25.0, emergency_vehicle_id="amb_1")
    rec = make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                              EventPriority.EMERGENCY, signal_actions=[action])
    city = _city(emergencies=[make_emergency()])

    engine.decide(city, [rec], timestamp_s=10.0)
    later = engine.decide(city, [rec],
                          timestamp_s=10.0 + DecisionSettings().signal_action_cooldown_s + 1)

    assert later[0].decision_type is DecisionType.EMERGENCY_PRIORITY


def test_two_emergency_vehicles_on_one_light_produce_one_action(engine):
    short = make_signal_action("A1", 12.0, action_id="a-short", emergency_vehicle_id="amb_1")
    long = make_signal_action("A1", 28.0, action_id="a-long", emergency_vehicle_id="amb_2")
    recs = [
        make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                            EventPriority.EMERGENCY, recommendation_id="r1",
                            signal_actions=[short]),
        make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                            EventPriority.EMERGENCY, recommendation_id="r2",
                            signal_actions=[long]),
    ]

    decisions = engine.decide(_city(emergencies=[make_emergency(), make_emergency("amb_2")]), recs)

    assert len(decisions[0].signal_actions) == 1
    assert decisions[0].signal_actions[0].duration_s == 28.0


# --------------------------------------------------------------------------- #
# Priority 2: traffic
# --------------------------------------------------------------------------- #
def test_traffic_action_on_congested_junction_is_kept(engine):
    action = make_signal_action("A1", 30.0, source_agent=AgentName.TRAFFIC_MIND)
    rec = make_recommendation(AgentName.TRAFFIC_MIND, DecisionType.TRAFFIC_OPTIMIZATION,
                              EventPriority.TRAFFIC, signal_actions=[action])

    decisions = engine.decide(_city(traffic=_traffic_state({"A1": "HIGH"})), [rec])

    assert decisions[0].decision_type is DecisionType.TRAFFIC_OPTIMIZATION
    assert decisions[0].signal_actions[0].tls_id == "A1"


def test_traffic_action_on_uncongested_junction_is_dropped(engine):
    action = make_signal_action("A1", 30.0, source_agent=AgentName.TRAFFIC_MIND)
    rec = make_recommendation(AgentName.TRAFFIC_MIND, DecisionType.TRAFFIC_OPTIMIZATION,
                              EventPriority.TRAFFIC, signal_actions=[action])

    decisions = engine.decide(_city(traffic=_traffic_state({"A1": "LOW"})), [rec])

    assert decisions[0].decision_type is DecisionType.NO_ACTION


def test_traffic_action_is_rate_limited_per_light(engine):
    action = make_signal_action("A1", 30.0, source_agent=AgentName.TRAFFIC_MIND)
    rec = make_recommendation(AgentName.TRAFFIC_MIND, DecisionType.TRAFFIC_OPTIMIZATION,
                              EventPriority.TRAFFIC, signal_actions=[action])
    city = _city(traffic=_traffic_state({"A1": "HIGH"}))

    engine.decide(city, [rec], timestamp_s=10.0)
    too_soon = engine.decide(city, [rec], timestamp_s=12.0)
    assert too_soon[0].decision_type is DecisionType.NO_ACTION

    later = engine.decide(city, [rec],
                          timestamp_s=10.0 + DecisionSettings().traffic_cooldown_s + 1)
    assert later[0].decision_type is DecisionType.TRAFFIC_OPTIMIZATION


# --------------------------------------------------------------------------- #
# Priority 3: parking
# --------------------------------------------------------------------------- #
def test_parking_recommendation_is_advisory_only(engine):
    rec = make_recommendation(
        AgentName.PARK_SENSE, DecisionType.PARKING_RECOMMENDATION, EventPriority.PARKING,
        parking_recommendations=[
            ParkingRecommendation(zone_id="PA_B", available=8, occupancy_pct=33.0, score=0.9),
            ParkingRecommendation(zone_id="PA_A", available=0, occupancy_pct=100.0, score=0.1),
        ])

    decisions = engine.decide(_city(), [rec])

    assert len(decisions) == 1
    parking = decisions[0]
    assert parking.decision_type is DecisionType.PARKING_RECOMMENDATION
    assert parking.signal_actions == []
    assert parking.applied is True
    assert [p.zone_id for p in parking.parking_recommendations] == ["PA_B", "PA_A"]
    assert "cannot change a traffic signal" in parking.explanation


def test_parking_never_competes_with_emergency(engine):
    emergency_action = make_signal_action("A1", 25.0, emergency_vehicle_id="amb_1")
    recs = [
        make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                            EventPriority.EMERGENCY, signal_actions=[emergency_action]),
        make_recommendation(AgentName.PARK_SENSE, DecisionType.PARKING_RECOMMENDATION,
                            EventPriority.PARKING,
                            parking_recommendations=[
                                ParkingRecommendation(zone_id="PA_B", available=8,
                                                      occupancy_pct=33.0, score=0.9)]),
    ]

    decisions = engine.decide(_city(emergencies=[make_emergency()]), recs)

    assert [d.decision_type for d in decisions] == [
        DecisionType.EMERGENCY_PRIORITY, DecisionType.PARKING_RECOMMENDATION]
    assert all(not d.signal_actions for d in decisions
               if d.decision_type is DecisionType.PARKING_RECOMMENDATION)


# --------------------------------------------------------------------------- #
# Determinism and ordering
# --------------------------------------------------------------------------- #
def test_decisions_are_ordered_by_priority(engine):
    emergency_action = make_signal_action("A1", 25.0, emergency_vehicle_id="amb_1")
    traffic_action = make_signal_action("B1", 30.0, source_agent=AgentName.TRAFFIC_MIND)
    recs = [
        make_recommendation(AgentName.PARK_SENSE, DecisionType.PARKING_RECOMMENDATION,
                            EventPriority.PARKING,
                            parking_recommendations=[
                                ParkingRecommendation(zone_id="PA_B", available=8,
                                                      occupancy_pct=33.0, score=0.9)]),
        make_recommendation(AgentName.TRAFFIC_MIND, DecisionType.TRAFFIC_OPTIMIZATION,
                            EventPriority.TRAFFIC, signal_actions=[traffic_action]),
        make_recommendation(AgentName.GREEN_CORRIDOR, DecisionType.EMERGENCY_PRIORITY,
                            EventPriority.EMERGENCY, signal_actions=[emergency_action]),
    ]
    city = _city(traffic=_traffic_state({"A1": "HIGH", "B1": "HIGH"}),
                 emergencies=[make_emergency()])

    decisions = engine.decide(city, recs)

    assert [int(d.priority) for d in decisions] == sorted(int(d.priority) for d in decisions)


def test_same_inputs_give_same_actions(engine):
    action = make_signal_action("A1", 30.0, source_agent=AgentName.TRAFFIC_MIND)
    rec = make_recommendation(AgentName.TRAFFIC_MIND, DecisionType.TRAFFIC_OPTIMIZATION,
                              EventPriority.TRAFFIC, signal_actions=[action])
    city = _city(traffic=_traffic_state({"A1": "HIGH"}))

    first = engine.decide(city, [rec], timestamp_s=10.0)
    engine.reset()
    second = engine.decide(city, [rec], timestamp_s=10.0)

    assert [(d.decision_type, [a.tls_id for a in d.signal_actions]) for d in first] == \
           [(d.decision_type, [a.tls_id for a in d.signal_actions]) for d in second]
