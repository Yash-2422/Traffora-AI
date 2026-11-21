"""Unit tests for the event manager."""

from __future__ import annotations

import pytest

from backend.orchestrator.event_manager import EventManager
from backend.orchestrator.exceptions import EventValidationError
from backend.orchestrator.schemas import (
    AgentName,
    EventPriority,
    EventStatus,
    EventType,
)


def test_priority_is_derived_from_event_type():
    assert EventManager.priority_for(EventType.EMERGENCY_DETECTED) is EventPriority.EMERGENCY
    assert EventManager.priority_for(EventType.SIGNAL_PRIORITY_REQUEST) is EventPriority.EMERGENCY
    assert EventManager.priority_for(EventType.CONGESTION_DETECTED) is EventPriority.TRAFFIC
    assert EventManager.priority_for(EventType.CONGESTION_PREDICTED) is EventPriority.TRAFFIC
    assert EventManager.priority_for(EventType.PARKING_DEMAND_ALERT) is EventPriority.PARKING


def test_create_event_sets_priority_and_validates_payload():
    manager = EventManager()
    event = manager.create_event(
        EventType.SIGNAL_PRIORITY_REQUEST, AgentName.GREEN_CORRIDOR, 10.0,
        payload={"vehicle_id": "amb_1", "tls_id": "A1"})
    assert event.priority is EventPriority.EMERGENCY
    assert event.status is EventStatus.PENDING
    assert event.event_id


def test_missing_payload_field_is_rejected():
    manager = EventManager()
    with pytest.raises(EventValidationError):
        manager.create_event(EventType.SIGNAL_PRIORITY_REQUEST, AgentName.GREEN_CORRIDOR, 1.0,
                             payload={"vehicle_id": "amb_1"})  # no tls_id


def test_congestion_event_accepts_edge_or_junction():
    manager = EventManager()
    edge_event = manager.create_event(EventType.CONGESTION_DETECTED, AgentName.TRAFFIC_MIND, 1.0,
                                      payload={"edge_id": "A1B1", "level": "HIGH"})
    assert edge_event.payload["edge_id"] == "A1B1"
    with pytest.raises(EventValidationError):
        manager.create_event(EventType.CONGESTION_DETECTED, AgentName.TRAFFIC_MIND, 1.0,
                             payload={"level": "HIGH"})


def test_negative_timestamp_is_rejected():
    manager = EventManager()
    with pytest.raises(EventValidationError):
        manager.create_event(EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, -1.0,
                             payload={"vehicle_id": "amb_1"})


def test_duplicate_within_window_is_suppressed():
    manager = EventManager(dedupe_window_s=30.0)
    first = manager.create_event(EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, 5.0,
                                 payload={"vehicle_id": "amb_1"})
    manager.publish(first)

    duplicate = manager.create_event(EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, 10.0,
                                     payload={"vehicle_id": "amb_1"})
    assert manager.try_publish(duplicate) is False
    assert manager.duplicate_count == 1
    assert manager.pending_count == 1


def test_same_event_after_window_is_accepted():
    manager = EventManager(dedupe_window_s=10.0)
    manager.publish(manager.create_event(EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, 1.0,
                                         payload={"vehicle_id": "amb_1"}))
    later = manager.create_event(EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, 40.0,
                                 payload={"vehicle_id": "amb_1"})
    assert manager.try_publish(later) is True
    assert manager.pending_count == 2


def test_drain_returns_emergency_before_traffic_before_parking():
    manager = EventManager()
    manager.publish(manager.create_event(EventType.PARKING_DEMAND_ALERT, AgentName.PARK_SENSE, 1.0,
                                         payload={"zone_id": "PA_A", "occupancy_pct": 95.0}))
    manager.publish(manager.create_event(EventType.CONGESTION_DETECTED, AgentName.TRAFFIC_MIND, 1.0,
                                         payload={"junction_id": "A1", "level": "HIGH"}))
    manager.publish(manager.create_event(EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, 1.0,
                                         payload={"vehicle_id": "amb_1"}))

    drained = manager.drain()
    assert [e.priority for e in drained] == [
        EventPriority.EMERGENCY, EventPriority.TRAFFIC, EventPriority.PARKING]
    assert manager.pending_count == 0


def test_fifo_within_same_priority():
    manager = EventManager()
    for i in range(3):
        manager.publish(manager.create_event(
            EventType.CONGESTION_DETECTED, AgentName.TRAFFIC_MIND, float(i),
            payload={"junction_id": f"J{i}", "level": "HIGH"}))
    assert [e.payload["junction_id"] for e in manager.drain()] == ["J0", "J1", "J2"]


def test_max_pending_is_enforced():
    manager = EventManager(max_pending=2)
    for i in range(3):
        event = manager.create_event(EventType.CONGESTION_DETECTED, AgentName.TRAFFIC_MIND,
                                     float(i), payload={"junction_id": f"J{i}"})
        manager.try_publish(event)
    assert manager.pending_count == 2
    assert manager.rejected_count == 1


def test_mark_processed_moves_event_to_history():
    manager = EventManager()
    event = manager.publish(manager.create_event(
        EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, 1.0,
        payload={"vehicle_id": "amb_1"}))
    manager.drain()
    manager.mark_processed(event)

    assert event.status is EventStatus.PROCESSED
    assert manager.processed_count == 1
    assert manager.history()[-1].event_id == event.event_id


def test_mark_failed_records_error():
    manager = EventManager()
    event = manager.publish(manager.create_event(
        EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR, 1.0,
        payload={"vehicle_id": "amb_1"}))
    manager.mark_processed(event, EventStatus.FAILED, error="boom")
    assert event.status is EventStatus.FAILED
    assert event.error == "boom"


def test_publish_many_counts_accepted():
    manager = EventManager()
    events = [manager.create_event(EventType.CONGESTION_DETECTED, AgentName.TRAFFIC_MIND,
                                   float(i), payload={"junction_id": f"J{i}"})
              for i in range(4)]
    assert manager.publish_many(events) == 4
    assert manager.pending_count == 4


def test_stats_and_clear():
    manager = EventManager()
    manager.publish(manager.create_event(EventType.EMERGENCY_DETECTED, AgentName.GREEN_CORRIDOR,
                                         1.0, payload={"vehicle_id": "amb_1"}))
    stats = manager.stats()
    assert stats["pending"] == 1
    manager.clear()
    assert manager.pending_count == 0
    assert manager.stats()["pending"] == 0
