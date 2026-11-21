"""Shared fixtures and fakes for the orchestrator tests.

Nothing here touches SUMO: the fakes implement the same ``CoordinatorLike``
protocol the real :class:`SimulationCoordinator` does, so the whole control
cycle can be exercised deterministically.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.orchestrator.agent_registry import AgentAdapter  # noqa: E402
from backend.orchestrator.schemas import (  # noqa: E402
    AgentHealth,
    AgentName,
    AgentRecommendation,
    AgentStatus,
    DecisionType,
    EmergencyEvent,
    EventPriority,
    ParkingState,
    ParkingZoneState,
    SignalAction,
    TrafficState,
)


class FakeConnection:
    """Records every TraCI write so tests can assert on it."""

    def __init__(self, tls_ids=("A1", "B1", "C1")) -> None:
        self.writes: List[tuple[str, float]] = []
        self._tls_ids = list(tls_ids)

        class _TrafficLight:
            def __init__(self, outer: "FakeConnection") -> None:
                self._outer = outer

            def getIDList(self):  # noqa: N802 - TraCI naming
                return list(self._outer._tls_ids)

            def setPhaseDuration(self, tls_id, duration):  # noqa: N802
                self._outer.writes.append((tls_id, float(duration)))

            def setPhase(self, *args, **kwargs):  # pragma: no cover - must never run
                raise AssertionError("setPhase must never be called")

            def setRedYellowGreenState(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("setRedYellowGreenState must never be called")

        class _Simulation:
            def __init__(self) -> None:
                self._time = 0.0

            def getTime(self):  # noqa: N802
                return self._time

        self.trafficlight = _TrafficLight(self)
        self.simulation = _Simulation()


class FakeCoordinator:
    """Deterministic stand-in for :class:`SimulationCoordinator`."""

    def __init__(self, tls_ids=("A1", "B1", "C1"), occupancy: Optional[Dict[str, int]] = None) -> None:
        self._tls_ids = list(tls_ids)
        self._time = 0.0
        self.running = True
        self.connection = FakeConnection(self._tls_ids)
        self.simulation = object()
        self._occupancy = occupancy or {"PA_A": 2, "PA_B": 5}
        self.steps = 0
        self.applied: List[SignalAction] = []
        self._states: Dict[str, Dict[str, Any]] = {}
        for tls in self._tls_ids:
            self._states[tls] = {
                "tls_id": tls,
                "phase": 0,
                "elapsed_s": 5.0,
                "remaining_s": 20.0,
                "num_phases": 4,
                "phase_kind": "GREEN",
                "state": "GGGrrr",
                "min_dur": 15.0,
                "max_dur": 60.0,
                "sim_time_s": 0.0,
            }

    # -- CoordinatorLike ------------------------------------------------- #
    @property
    def is_running(self) -> bool:
        return self.running

    @property
    def current_time(self) -> float:
        return self._time

    @property
    def known_tls_ids(self):
        return set(self._tls_ids)

    def signal_state(self, tls_id: str):
        state = self._states.get(tls_id)
        return dict(state, sim_time_s=self._time) if state else None

    def signal_states(self, tls_ids=None):
        ids = tls_ids or self._tls_ids
        return {tls: self.signal_state(tls) for tls in ids}

    def read_parking_occupancy(self):
        return dict(self._occupancy)

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        self.running = True

    # -- test helpers ---------------------------------------------------- #
    def step(self, n: int = 1) -> float:
        self._time += n
        self.steps += n
        self.connection.simulation._time = self._time
        for state in self._states.values():
            state["elapsed_s"] += n
            state["remaining_s"] = max(0.0, state["remaining_s"] - n)
        return self._time

    def set_state(self, tls_id: str, **updates: Any) -> None:
        self._states[tls_id].update(updates)

    def apply(self, actions, validations):
        applied = []
        for action in actions:
            result = validations.get(action.action_id)
            if result is None or not result.valid:
                raise AssertionError("apply() received an unvalidated action")
            self.connection.trafficlight.setPhaseDuration(action.tls_id, action.duration_s)
            applied.append(action)
        self.applied.extend(applied)
        return [{"action_id": a.action_id, "tls_id": a.tls_id} for a in applied]

    def collect_metrics(self):
        return {
            "vehicles_departed": float(10 + self.steps),
            "vehicles_arrived": float(self.steps),
            "avg_waiting_time_s": 2.5,
            "avg_queue_length_veh": 0.5,
            "avg_travel_time_s": 100.0,
            "emergency_travel_time_s": 0.0,
        }

    def snapshot_summary(self):
        """Mirror ``SimulationCoordinator.snapshot_summary`` for the API tests."""
        return {
            "running": self.running,
            "ended": False,
            "label": "fake",
            "sumocfg": "fake.sumocfg",
            "sim_time_s": self._time if self.running else None,
            "traffic_lights": len(self._tls_ids),
            "parking_areas": len(self._occupancy),
            "applied_signal_actions": len(self.applied),
        }

    def close(self) -> None:
        self.running = False


class FakeAdapter(AgentAdapter):
    """Minimal AgentAdapter replacement that returns a canned snapshot.

    Subclasses :class:`AgentAdapter` rather than duck-typing it, because
    ``AgentRegistry.register`` deliberately type-checks its argument: an
    adapter that is not an ``AgentAdapter`` is a programming error, not a
    supported extension point.
    """

    def __init__(self, name: AgentName, snapshot_factory=None, fail_init: bool = False,
                 fail_collect: bool = False) -> None:
        super().__init__()
        self.name = name
        self._snapshot_factory = snapshot_factory
        self._fail_init = fail_init
        self._fail_collect = fail_collect
        self.initialized = False
        self._status = AgentStatus(agent=name, health=AgentHealth.UNKNOWN, available=False)

    def initialize(self, coordinator) -> None:
        if self._fail_init:
            self._status = AgentStatus(agent=self.name, health=AgentHealth.FAILED,
                                       available=False, message="boom")
            raise RuntimeError("boom")
        self.initialized = True
        self._status = AgentStatus(agent=self.name, health=AgentHealth.READY, available=True)

    def status(self) -> AgentStatus:
        return self._status

    def collect(self, coordinator, timestamp_s: float):
        from backend.orchestrator.agent_registry import AgentSnapshot

        if self._fail_collect:
            self._status = AgentStatus(agent=self.name, health=AgentHealth.DEGRADED,
                                       available=False, message="collect failed")
            return AgentSnapshot(agent=self.name, notes=["collect failed"])
        if self._snapshot_factory is None:
            return AgentSnapshot(agent=self.name)
        return self._snapshot_factory(timestamp_s)

    def shutdown(self) -> None:
        self._status = AgentStatus(agent=self.name, health=AgentHealth.STOPPED, available=False)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def make_signal_action(tls_id: str = "A1", duration: float = 20.0, **kwargs) -> SignalAction:
    return SignalAction(
        action_id=kwargs.pop("action_id", f"act-{tls_id}-{duration}"),
        tls_id=tls_id,
        duration_s=duration,
        phase_index=kwargs.pop("phase_index", 0),
        source_agent=kwargs.pop("source_agent", AgentName.GREEN_CORRIDOR),
        created_at_s=kwargs.pop("created_at_s", 0.0),
        **kwargs,
    )


def make_emergency(vehicle_id: str = "amb_1", tls_id: str = "A1", eta: float = 8.0) -> EmergencyEvent:
    return EmergencyEvent(
        event_id=f"emg-{vehicle_id}",
        vehicle_id=vehicle_id,
        timestamp_s=0.0,
        tls_id=tls_id,
        distance_m=60.0,
        eta_s=eta,
        route=["A0A1", "A1B1"],
    )


def make_traffic(levels: Optional[Dict[str, str]] = None) -> TrafficState:
    return TrafficState(
        timestamp_s=0.0,
        vehicle_count=100,
        average_waiting_time_s=3.0,
        average_queue_length_m=10.0,
        congestion_level="HIGH",
        junction_levels=levels or {"A1": "HIGH", "B1": "LOW"},
    )


def make_parking(occupancy: float = 85.0) -> ParkingState:
    occupied = int(occupancy / 10)
    return ParkingState(
        timestamp_s=0.0,
        zones=[ParkingZoneState(zone_id="PA_A", capacity=10, occupied=occupied,
                                available=10 - occupied, occupancy_pct=occupancy)],
        total_capacity=10,
        total_occupied=occupied,
        total_available=10 - occupied,
        overall_occupancy_pct=occupancy,
    )


def make_recommendation(agent: AgentName, decision_type: DecisionType,
                        priority: EventPriority, **kwargs) -> AgentRecommendation:
    return AgentRecommendation(
        recommendation_id=kwargs.pop("recommendation_id", f"rec-{agent.value}"),
        agent=agent,
        decision_type=decision_type,
        priority=priority,
        **kwargs,
    )


@pytest.fixture
def fake_coordinator() -> FakeCoordinator:
    return FakeCoordinator()
