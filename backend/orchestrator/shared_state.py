"""Thread-safe shared state for the orchestrator.

The state object is deliberately *not* a module-level global: the orchestrator
owns exactly one instance and passes it to the modules that need it. A single
re-entrant lock protects every read and write, so the FastAPI layer can poll
state while the simulation loop is running.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional

from .exceptions import SharedStateError
from .schemas import (
    AgentStatus,
    CityState,
    EmergencyEvent,
    OrchestratorDecision,
    ParkingState,
    SystemMetrics,
    TrafficState,
)

logger = logging.getLogger(__name__)


class SharedState:
    """Latest known traffic, emergency, parking, agent and decision state."""

    def __init__(self, decision_history_size: int = 500) -> None:
        if decision_history_size < 1:
            raise SharedStateError("decision_history_size must be >= 1")
        self._lock = threading.RLock()
        self._traffic: Optional[TrafficState] = None
        self._parking: Optional[ParkingState] = None
        self._emergencies: Dict[str, EmergencyEvent] = {}
        self._agents: Dict[str, AgentStatus] = {}
        self._decisions: Deque[OrchestratorDecision] = deque(maxlen=decision_history_size)
        self._latest_decision: Optional[OrchestratorDecision] = None
        self._sim_time_s: float = 0.0
        self._metrics = SystemMetrics()
        self._metric_samples: Dict[str, List[float]] = {
            "waiting_time": [],
            "queue_length": [],
            "travel_time": [],
        }
        self._latencies: List[float] = []

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def set_simulation_time(self, timestamp_s: float) -> None:
        with self._lock:
            self._sim_time_s = max(self._sim_time_s, float(timestamp_s))

    def update_traffic(self, traffic: TrafficState) -> None:
        if not isinstance(traffic, TrafficState):
            raise SharedStateError("update_traffic expects a TrafficState")
        with self._lock:
            self._traffic = traffic
            self._sim_time_s = max(self._sim_time_s, traffic.timestamp_s)

    def update_parking(self, parking: ParkingState) -> None:
        if not isinstance(parking, ParkingState):
            raise SharedStateError("update_parking expects a ParkingState")
        with self._lock:
            self._parking = parking
            self._sim_time_s = max(self._sim_time_s, parking.timestamp_s)

    def upsert_emergency(self, event: EmergencyEvent) -> None:
        if not isinstance(event, EmergencyEvent):
            raise SharedStateError("upsert_emergency expects an EmergencyEvent")
        with self._lock:
            self._emergencies[event.vehicle_id] = event
            self._sim_time_s = max(self._sim_time_s, event.timestamp_s)

    def remove_emergency(self, vehicle_id: str) -> None:
        with self._lock:
            self._emergencies.pop(vehicle_id, None)

    def clear_emergencies(self) -> None:
        with self._lock:
            self._emergencies.clear()

    def set_agent_status(self, status: AgentStatus) -> None:
        if not isinstance(status, AgentStatus):
            raise SharedStateError("set_agent_status expects an AgentStatus")
        with self._lock:
            self._agents[status.agent.value] = status

    def record_decision(self, decision: OrchestratorDecision) -> None:
        if not isinstance(decision, OrchestratorDecision):
            raise SharedStateError("record_decision expects an OrchestratorDecision")
        with self._lock:
            self._decisions.append(decision)
            self._latest_decision = decision
            self._sim_time_s = max(self._sim_time_s, decision.timestamp_s)
            # ``NO_ACTION`` is not an action: it must not inflate the counters.
            if decision.decision_type.value != "NO_ACTION":
                if decision.applied:
                    self._metrics.accepted_decisions += 1
                else:
                    self._metrics.rejected_decisions += 1

    def record_metric_sample(self, waiting_time_s: Optional[float] = None,
                             queue_length_m: Optional[float] = None,
                             travel_time_s: Optional[float] = None) -> None:
        """Accumulate one measured sample (used for run averages)."""
        with self._lock:
            if waiting_time_s is not None and waiting_time_s >= 0:
                self._metric_samples["waiting_time"].append(float(waiting_time_s))
            if queue_length_m is not None and queue_length_m >= 0:
                self._metric_samples["queue_length"].append(float(queue_length_m))
            if travel_time_s is not None and travel_time_s >= 0:
                self._metric_samples["travel_time"].append(float(travel_time_s))

    def record_latency(self, latency_ms: float) -> None:
        if latency_ms < 0:
            return
        with self._lock:
            self._latencies.append(float(latency_ms))

    def set_metric(self, name: str, value: Optional[float]) -> None:
        """Set a scalar metric on :class:`SystemMetrics` by field name."""
        with self._lock:
            if not hasattr(self._metrics, name):
                raise SharedStateError(f"Unknown metric: {name}")
            setattr(self._metrics, name, value)

    def increment_metric(self, name: str, amount: int = 1) -> None:
        with self._lock:
            if not hasattr(self._metrics, name):
                raise SharedStateError(f"Unknown metric: {name}")
            setattr(self._metrics, name, getattr(self._metrics, name) + amount)

    def add_note(self, note: str) -> None:
        with self._lock:
            self._metrics.notes.append(note)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get_traffic(self) -> Optional[TrafficState]:
        with self._lock:
            return self._traffic

    def get_parking(self) -> Optional[ParkingState]:
        with self._lock:
            return self._parking

    def get_emergency_vehicles(self) -> List[EmergencyEvent]:
        with self._lock:
            return sorted(self._emergencies.values(), key=lambda e: e.vehicle_id)

    def get_emergency(self, vehicle_id: str) -> Optional[EmergencyEvent]:
        with self._lock:
            return self._emergencies.get(vehicle_id)

    def get_agent_statuses(self) -> List[AgentStatus]:
        with self._lock:
            return sorted(self._agents.values(), key=lambda s: s.agent.value)

    def get_agent_status(self, agent: str) -> Optional[AgentStatus]:
        with self._lock:
            return self._agents.get(agent)

    def get_latest_decision(self) -> Optional[OrchestratorDecision]:
        with self._lock:
            return self._latest_decision

    def get_decisions(self, limit: Optional[int] = None) -> List[OrchestratorDecision]:
        with self._lock:
            items = list(self._decisions)
        if limit is not None:
            items = items[-limit:]
        return items

    @property
    def simulation_time_s(self) -> float:
        with self._lock:
            return self._sim_time_s

    def snapshot(self) -> CityState:
        """A consistent copy of the whole city state."""
        with self._lock:
            # Deep copies: a caller that mutates the returned snapshot must not
            # be able to corrupt the shared state behind everyone else's back.
            return CityState(
                timestamp_s=self._sim_time_s,
                traffic=self._traffic.model_copy(deep=True) if self._traffic else None,
                emergency_vehicles=[e.model_copy(deep=True) for e in
                                    sorted(self._emergencies.values(),
                                           key=lambda e: e.vehicle_id)],
                parking=self._parking.model_copy(deep=True) if self._parking else None,
                agents=[s.model_copy(deep=True) for s in
                        sorted(self._agents.values(), key=lambda s: s.agent.value)],
                latest_decision=(self._latest_decision.model_copy(deep=True)
                                 if self._latest_decision else None),
            )

    def metrics(self) -> SystemMetrics:
        """Averaged metrics plus the latest measured scalars."""
        with self._lock:
            metrics = self._metrics.model_copy(deep=True)
            metrics.simulation_time_s = self._sim_time_s
            for name, samples in self._metric_samples.items():
                if samples:
                    average = sum(samples) / len(samples)
                    if name == "waiting_time":
                        metrics.average_waiting_time_s = round(average, 3)
                    elif name == "queue_length":
                        metrics.average_queue_length_m = round(average, 3)
                    elif name == "travel_time":
                        metrics.average_travel_time_s = round(average, 3)
            if self._latencies:
                metrics.orchestrator_processing_latency_ms = round(
                    sum(self._latencies) / len(self._latencies), 3)
            if self._agents:
                available = sum(1 for s in self._agents.values() if s.available)
                metrics.agent_availability_pct = round(available / len(self._agents) * 100, 1)
            return metrics

    def agent_availability_pct(self) -> Optional[float]:
        statuses = self.get_agent_statuses()
        if not statuses:
            return None
        available = sum(1 for status in statuses if status.available)
        return round(available / len(statuses) * 100, 1)

    def reset(self) -> None:
        """Forget everything (used between comparison runs)."""
        with self._lock:
            self._traffic = None
            self._parking = None
            self._emergencies.clear()
            self._agents.clear()
            self._decisions.clear()
            self._latest_decision = None
            self._sim_time_s = 0.0
            self._metrics = SystemMetrics()
            for samples in self._metric_samples.values():
                samples.clear()
            self._latencies.clear()
