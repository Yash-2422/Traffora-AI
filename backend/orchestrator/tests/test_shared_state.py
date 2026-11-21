"""Unit tests for the shared state manager."""

from __future__ import annotations

import threading

import pytest

from backend.orchestrator.exceptions import SharedStateError
from backend.orchestrator.schemas import (
    AgentHealth,
    AgentName,
    AgentStatus,
    DecisionType,
    EventPriority,
    OrchestratorDecision,
    SignalAction,
)
from backend.orchestrator.shared_state import SharedState

from .conftest import make_emergency, make_parking, make_traffic


def _decision(decision_type: DecisionType = DecisionType.TRAFFIC_OPTIMIZATION,
              actions=None) -> OrchestratorDecision:
    return OrchestratorDecision(
        decision_id=f"dec-{decision_type.value}",
        timestamp_s=1.0,
        decision_type=decision_type,
        priority=EventPriority.TRAFFIC,
        signal_actions=actions or [],
    )


def test_traffic_and_parking_updates_are_visible():
    state = SharedState()
    state.update_traffic(make_traffic())
    state.update_parking(make_parking(70.0))

    assert state.get_traffic() is not None
    assert state.get_traffic().congestion_level == "HIGH"
    assert state.get_parking().overall_occupancy_pct == pytest.approx(70.0)


def test_emergency_upsert_and_remove():
    state = SharedState()
    state.upsert_emergency(make_emergency("amb_1"))
    state.upsert_emergency(make_emergency("amb_2", tls_id="B1"))
    assert {e.vehicle_id for e in state.get_emergency_vehicles()} == {"amb_1", "amb_2"}

    # Upserting the same vehicle replaces it rather than duplicating it.
    state.upsert_emergency(make_emergency("amb_1", tls_id="C1"))
    assert len(state.get_emergency_vehicles()) == 2
    assert state.get_emergency("amb_1").tls_id == "C1"

    state.remove_emergency("amb_1")
    assert state.get_emergency("amb_1") is None
    assert len(state.get_emergency_vehicles()) == 1


def test_agent_status_requires_agent_status_model():
    state = SharedState()
    with pytest.raises(SharedStateError):
        state.set_agent_status({"agent": "traffic_mind"})  # type: ignore[arg-type]

    state.set_agent_status(AgentStatus(agent=AgentName.TRAFFIC_MIND,
                                       health=AgentHealth.READY, available=True))
    assert state.get_agent_status("traffic_mind").available is True
    assert state.get_agent_status(AgentName.TRAFFIC_MIND).health is AgentHealth.READY


def test_decision_history_and_latest():
    state = SharedState(decision_history_size=3)
    for i in range(5):
        state.record_decision(OrchestratorDecision(
            decision_id=f"dec-{i}", timestamp_s=float(i),
            decision_type=DecisionType.NO_ACTION, priority=EventPriority.PARKING))

    assert state.get_latest_decision().decision_id == "dec-4"
    assert len(state.get_decisions()) == 3
    assert len(state.get_decisions(limit=2)) == 2


def test_no_action_decisions_are_not_counted_as_accepted():
    state = SharedState()
    state.record_decision(_decision(DecisionType.NO_ACTION))
    assert state.metrics().accepted_decisions == 0
    assert state.metrics().rejected_decisions == 0

    state.record_decision(_decision(DecisionType.TRAFFIC_OPTIMIZATION, actions=[]))
    assert state.metrics().accepted_decisions == 0  # not applied yet

    applied = _decision(DecisionType.TRAFFIC_OPTIMIZATION, actions=[])
    applied.applied = True
    state.record_decision(applied)
    assert state.metrics().accepted_decisions == 1


def test_rejected_decision_is_counted():
    state = SharedState()
    rejected = _decision(DecisionType.TRAFFIC_OPTIMIZATION, actions=[])
    rejected.applied = False
    rejected.rejected_reason = "unsafe"
    state.record_decision(rejected)
    assert state.metrics().rejected_decisions == 1
    assert state.metrics().accepted_decisions == 0


def test_metrics_aggregate_samples_and_latency():
    state = SharedState()
    state.record_metric_sample(waiting_time_s=2.0, queue_length_m=4.0, travel_time_s=100.0)
    state.record_metric_sample(waiting_time_s=4.0, queue_length_m=6.0, travel_time_s=120.0)
    state.record_latency(10.0)
    state.record_latency(30.0)

    metrics = state.metrics()
    assert metrics.average_waiting_time_s == pytest.approx(3.0)
    assert metrics.average_queue_length_m == pytest.approx(5.0)
    assert metrics.average_travel_time_s == pytest.approx(110.0)
    assert metrics.orchestrator_processing_latency_ms == pytest.approx(20.0)


def test_agent_availability_percentage():
    state = SharedState()
    state.set_agent_status(AgentStatus(agent=AgentName.TRAFFIC_MIND,
                                       health=AgentHealth.READY, available=True))
    state.set_agent_status(AgentStatus(agent=AgentName.PARK_SENSE,
                                       health=AgentHealth.FAILED, available=False))
    assert state.metrics().agent_availability_pct == pytest.approx(50.0)


def test_snapshot_is_a_consistent_copy():
    state = SharedState()
    state.update_traffic(make_traffic())
    state.upsert_emergency(make_emergency())
    state.record_decision(_decision())

    snap = state.snapshot()
    assert snap.traffic is not None
    assert len(snap.emergency_vehicles) == 1
    assert snap.latest_decision is not None

    # Mutating the snapshot must not mutate the shared state.
    snap.traffic.junction_levels["A1"] = "LOW"
    assert state.get_traffic().junction_levels["A1"] == "HIGH"


def test_reset_clears_everything():
    state = SharedState()
    state.update_traffic(make_traffic())
    state.record_decision(_decision())
    state.reset()
    assert state.get_traffic() is None
    assert state.get_decisions() == []


def test_concurrent_updates_do_not_lose_data():
    state = SharedState()

    def worker(prefix: str) -> None:
        for i in range(50):
            state.upsert_emergency(make_emergency(f"{prefix}_{i}"))

    threads = [threading.Thread(target=worker, args=(p,)) for p in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(state.get_emergency_vehicles()) == 150
