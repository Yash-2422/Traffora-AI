"""Agent registry and integration adapters.

The three agents keep their own models, their own public functions and their
own standalone scenarios. This module is the *only* place where the
orchestrator touches them, and it does so through thin adapters that:

* call the documented public functions (never private agent internals),
* convert agent output into the canonical orchestrator schemas,
* isolate failures so one broken agent cannot stop the others,
* never let an agent write to SUMO directly (the coordinator owns the single
  TraCI connection and the safety validator gates every write).

GreenCorridor note
------------------
``PriorityManager.update()`` writes ``setPhaseDuration`` itself. Calling it
from the orchestrator would bypass :class:`SafetyValidator`, so the adapter
uses the agent's *read-only* ``IntersectionMonitor`` / ``EmergencyDetector``
and derives the same priority action as data. The action is then validated and
applied by the orchestrator. The agent's own ``PriorityManager`` remains
untouched and still works standalone.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

from .exceptions import AgentInitializationError, AgentNotFoundError
from .schemas import (
    ActionType,
    AgentHealth,
    AgentName,
    AgentRecommendation,
    AgentStatus,
    DecisionType,
    EmergencyEvent,
    Event,
    EventPriority,
    EventType,
    ParkingRecommendation,
    ParkingState,
    ParkingZoneState,
    SignalAction,
    TrafficState,
)

logger = logging.getLogger(__name__)

# GreenCorridor priority policy constants (mirroring its documented config).
PRIORITY_HORIZON_M = 250.0
PRIORITY_ETA_BUFFER_S = 2.0
PRIORITY_MAX_EXTENSION_S = 20.0
PRIORITY_MIN_GREEN_FLOOR_S = 4.0


class CoordinatorLike(Protocol):
    """The subset of :class:`SimulationCoordinator` the adapters need."""

    @property
    def is_running(self) -> bool: ...

    @property
    def current_time(self) -> float: ...

    @property
    def simulation(self) -> Any: ...

    @property
    def connection(self) -> Any: ...

    @property
    def known_tls_ids(self) -> Sequence[str]: ...

    def signal_state(self, tls_id: str) -> Dict[str, Any]: ...

    def read_parking_occupancy(self) -> Dict[str, int]: ...


@dataclass
class AgentSnapshot:
    """Everything one adapter produced in one sampling round."""

    agent: AgentName
    traffic: Optional[TrafficState] = None
    parking: Optional[ParkingState] = None
    emergencies: List[EmergencyEvent] = field(default_factory=list)
    recommendations: List[AgentRecommendation] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    arrived_emergencies: List[str] = field(default_factory=list)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _as_dict(payload: Any) -> Dict[str, Any]:
    """Normalise an agent result to a plain dict.

    The three agents return different shapes: TrafficMind uses Pydantic models,
    GreenCorridor and ParkSense use dataclasses with ``to_dict()``, and some
    helpers already return dicts. The orchestrator only ever works on dicts.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:  # pragma: no cover - older pydantic
            return dump()
    to_dict = getattr(payload, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    as_dict = getattr(payload, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return {}


def _error_code(payload: Dict[str, Any]) -> Optional[str]:
    if isinstance(payload, dict) and payload.get("status") == "error":
        return str(payload.get("code") or "AGENT_ERROR")
    return None


class AgentAdapter:
    """Base adapter: health tracking, latency measurement, safe calls."""

    name: AgentName = AgentName.TRAFFIC_MIND

    def __init__(self) -> None:
        self._status = AgentStatus(agent=self.name, health=AgentHealth.UNKNOWN,
                                   available=False, message="not initialised")

    # -- lifecycle ------------------------------------------------------- #
    def initialize(self, coordinator: CoordinatorLike) -> None:  # pragma: no cover
        raise NotImplementedError

    def shutdown(self) -> None:
        self._status = self._status.model_copy(
            update={"health": AgentHealth.STOPPED, "available": False,
                    "message": "stopped"})

    def status(self) -> AgentStatus:
        return self._status.model_copy(deep=True)

    # -- helpers --------------------------------------------------------- #
    def _mark_ready(self, message: str = "ready") -> None:
        self._status = AgentStatus(agent=self.name, health=AgentHealth.READY,
                                   available=True, message=message,
                                   last_update_s=self._status.last_update_s)

    def _mark_degraded(self, message: str) -> None:
        self._status = AgentStatus(agent=self.name, health=AgentHealth.DEGRADED,
                                   available=False, message=message,
                                   last_update_s=self._status.last_update_s)

    def _mark_failed(self, message: str) -> None:
        self._status = AgentStatus(agent=self.name, health=AgentHealth.FAILED,
                                   available=False, message=message,
                                   last_update_s=self._status.last_update_s)

    def _touch(self, timestamp_s: float, latency_ms: float,
               message: Optional[str] = None) -> None:
        self._status = self._status.model_copy(update={
            "last_update_s": timestamp_s,
            "latency_ms": round(latency_ms, 3),
            "message": message or self._status.message,
        })

    def collect(self, coordinator: CoordinatorLike, timestamp_s: float) -> AgentSnapshot:
        started = time.perf_counter()
        try:
            snapshot = self._collect(coordinator, timestamp_s)
            self._touch(timestamp_s, (time.perf_counter() - started) * 1000.0)
            return snapshot
        except Exception as exc:  # noqa: BLE001 - one agent must not break the loop
            latency = (time.perf_counter() - started) * 1000.0
            logger.warning("%s collection failed: %s", self.name.value, exc)
            self._mark_degraded(f"collection failed: {exc}")
            self._touch(timestamp_s, latency, f"collection failed: {exc}")
            return AgentSnapshot(agent=self.name, notes=[f"collection failed: {exc}"])

    def _collect(self, coordinator: CoordinatorLike, timestamp_s: float) -> AgentSnapshot:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# TrafficMind
# --------------------------------------------------------------------------- #
class TrafficMindAdapter(AgentAdapter):
    """Adapter for ``backend.agents.traffic_mind``."""

    name = AgentName.TRAFFIC_MIND

    def __init__(self, agent: Any = None, predict_every_s: float = 30.0) -> None:
        super().__init__()
        self._agent = agent
        self._predict_every_s = predict_every_s
        self._last_prediction_s: Optional[float] = None

    def initialize(self, coordinator: CoordinatorLike) -> None:
        try:
            if self._agent is None:
                from ..agents.traffic_mind.agent import get_agent
                self._agent = get_agent()
            if not coordinator.is_running:
                self._mark_degraded("simulation is not running")
                return
            self._agent.attach(coordinator.simulation)
            self._mark_ready("attached to shared simulation")
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(f"initialisation failed: {exc}")
            raise AgentInitializationError(
                f"TrafficMind could not attach: {exc}") from exc

    def _collect(self, coordinator: CoordinatorLike, timestamp_s: float) -> AgentSnapshot:
        snapshot = AgentSnapshot(agent=self.name)

        status = _as_dict(self._agent.get_traffic_status())
        code = _error_code(status)
        if code:
            self._mark_degraded(f"get_traffic_status: {code}")
            snapshot.notes.append(f"get_traffic_status failed: {code}")
            return snapshot

        congestion = _as_dict(self._agent.detect_congestion())
        code = _error_code(congestion)
        if code:
            snapshot.notes.append(f"detect_congestion failed: {code}")
            congestion = {}

        snapshot.traffic = self._to_traffic_state(status, congestion, timestamp_s)

        # Congestion alerts -> events (only newly raised ones).
        for alert in congestion.get("alerts", []) or []:
            if not alert.get("is_new"):
                continue
            snapshot.events.append(Event(
                event_id=_new_id("evt"),
                event_type=EventType.CONGESTION_DETECTED,
                source_agent=self.name,
                timestamp_s=timestamp_s,
                priority=EventPriority.TRAFFIC,
                payload={
                    "alert_id": alert.get("alert_id"),
                    "entity_type": alert.get("entity_type"),
                    "junction_id": alert.get("entity_id") if alert.get("entity_type") == "junction" else None,
                    "edge_id": alert.get("entity_id") if alert.get("entity_type") == "road" else None,
                    "level": alert.get("level"),
                    "score": alert.get("score"),
                    "severity": alert.get("severity"),
                    "message": alert.get("message"),
                },
            ))

        # Prediction alerts (best effort: the model may be unavailable).
        if self._should_predict(timestamp_s):
            prediction = _as_dict(self._agent.predict_congestion())
            self._last_prediction_s = timestamp_s
            code = _error_code(prediction)
            if code:
                snapshot.notes.append(f"predict_congestion unavailable: {code}")
            else:
                for warning in prediction.get("early_warnings", []) or []:
                    snapshot.events.append(Event(
                        event_id=_new_id("evt"),
                        event_type=EventType.CONGESTION_PREDICTED,
                        source_agent=self.name,
                        timestamp_s=timestamp_s,
                        priority=EventPriority.TRAFFIC,
                        payload={
                            "junction_id": warning.get("junction_id"),
                            "current_level": warning.get("current_level"),
                            "predicted_level": warning.get("predicted_level"),
                            "confidence": warning.get("confidence"),
                            "horizon_s": prediction.get("horizon_s"),
                            "message": warning.get("message"),
                        },
                    ))

        # Signal advice -> validated actions (only explicit green extensions).
        advice = _as_dict(self._agent.generate_signal_recommendation())
        code = _error_code(advice)
        if code:
            snapshot.notes.append(f"generate_signal_recommendation failed: {code}")
        else:
            actions, advisory = self._to_signal_actions(advice, coordinator, timestamp_s)
            if actions or advisory:
                snapshot.recommendations.append(AgentRecommendation(
                    recommendation_id=_new_id("rec"),
                    agent=self.name,
                    decision_type=DecisionType.TRAFFIC_OPTIMIZATION,
                    priority=EventPriority.TRAFFIC,
                    signal_actions=actions,
                    explanation=(
                        f"TrafficMind advised {len(advisory)} junction rebalancing plan(s) "
                        f"(advisory only) and {len(actions)} immediate green extension(s)."
                    ),
                    created_at_s=timestamp_s,
                    metadata={"advisory_junctions": advisory},
                ))
        return snapshot

    def _should_predict(self, timestamp_s: float) -> bool:
        if self._last_prediction_s is None:
            return True
        return timestamp_s - self._last_prediction_s >= self._predict_every_s

    @staticmethod
    def _to_traffic_state(status: Dict[str, Any], congestion: Dict[str, Any],
                          timestamp_s: float) -> TrafficState:
        network = status.get("network", {}) or {}
        edges = status.get("edges", []) or []
        queue_values = [float(e.get("queue_length_m", 0.0)) for e in edges]
        speed_values = [float(e.get("mean_speed_mps", 0.0)) for e in edges
                        if float(e.get("vehicle_count", 0)) > 0]
        summary = congestion.get("summary", {}) or {}
        junctions = congestion.get("junctions", []) or []
        levels = {j.get("junction_id"): str(j.get("level", "LOW")) for j in junctions}
        if summary.get("junctions_high", 0) > 0:
            overall = "HIGH"
        elif summary.get("junctions_medium", 0) > 0:
            overall = "MEDIUM"
        else:
            overall = "LOW"
        return TrafficState(
            timestamp_s=timestamp_s,
            vehicle_count=int(network.get("total_vehicles", 0)),
            average_speed_mps=round(sum(speed_values) / len(speed_values), 3) if speed_values else 0.0,
            average_waiting_time_s=round(float(network.get("mean_waiting_time_s", 0.0)), 3),
            average_queue_length_m=round(sum(queue_values) / len(queue_values), 3) if queue_values else 0.0,
            congestion_level=overall,
            congested_junctions=sorted(j.get("junction_id") for j in junctions
                                       if str(j.get("level")) in ("MEDIUM", "HIGH")),
            junction_levels=levels,
        )

    @staticmethod
    def _to_signal_actions(advice: Dict[str, Any], coordinator: CoordinatorLike,
                           timestamp_s: float) -> tuple[List[SignalAction], List[str]]:
        actions: List[SignalAction] = []
        advisory: List[str] = []
        for rec in advice.get("recommendations", []) or []:
            junction_id = rec.get("junction_id")
            if not junction_id:
                continue
            immediate = rec.get("immediate", {}) or {}
            if rec.get("action") == "rebalance":
                advisory.append(junction_id)
            if immediate.get("action") != "extend_green":
                continue
            extend_s = float(immediate.get("extend_s", 0.0) or 0.0)
            if extend_s <= 0:
                continue
            try:
                state = coordinator.signal_state(junction_id)
            except Exception:  # noqa: BLE001 - unknown TLS: leave it to the validator
                state = {}
            remaining = float(state.get("remaining_s", 0.0) or 0.0)
            duration = remaining + extend_s
            # Clamp to the program's declared maximum so the intent is realisable;
            # the safety validator still has the final say.
            program_max = float(state.get("max_dur", 0.0) or 0.0)
            if program_max > 0:
                duration = min(duration, program_max)
            actions.append(SignalAction(
                action_id=_new_id("act"),
                tls_id=junction_id,
                action_type=ActionType.SET_PHASE_DURATION,
                duration_s=round(duration, 3),
                phase_index=state.get("phase"),
                reason=f"TrafficMind: {immediate.get('reason') or 'extend green under demand'}",
                source_agent=AgentName.TRAFFIC_MIND,
                created_at_s=timestamp_s,
            ))
        return actions, advisory


# --------------------------------------------------------------------------- #
# GreenCorridor
# --------------------------------------------------------------------------- #
class GreenCorridorAdapter(AgentAdapter):
    """Adapter for ``backend.agents.green_corridor`` (read-only signal planning)."""

    name = AgentName.GREEN_CORRIDOR

    def __init__(self, detector: Any = None, monitor: Any = None,
                 lookahead_m: float = PRIORITY_HORIZON_M) -> None:
        super().__init__()
        self._detector = detector
        self._monitor = monitor
        self._lookahead_m = lookahead_m
        self._known_vehicles: set[str] = set()

    def initialize(self, coordinator: CoordinatorLike) -> None:
        try:
            if not coordinator.is_running:
                self._mark_degraded("simulation is not running")
                return
            if self._detector is None or self._monitor is None:
                from ..agents.green_corridor.emergency_detector import EmergencyDetector
                from ..agents.green_corridor.intersection_monitor import IntersectionMonitor
                connection = coordinator.connection
                self._detector = self._detector or EmergencyDetector(connection)
                self._monitor = self._monitor or IntersectionMonitor(connection)
            self._mark_ready("monitoring emergency vehicles")
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(f"initialisation failed: {exc}")
            raise AgentInitializationError(
                f"GreenCorridor could not initialise: {exc}") from exc

    def _collect(self, coordinator: CoordinatorLike, timestamp_s: float) -> AgentSnapshot:
        snapshot = AgentSnapshot(agent=self.name)
        update = self._detector.update() or {}
        for vehicle_id in update.get("new", []) or []:
            snapshot.events.append(Event(
                event_id=_new_id("evt"),
                event_type=EventType.EMERGENCY_DETECTED,
                source_agent=self.name,
                timestamp_s=timestamp_s,
                priority=EventPriority.EMERGENCY,
                payload={"vehicle_id": vehicle_id},
            ))
        snapshot.arrived_emergencies = list(update.get("arrived", []) or [])

        active = self._detector.detect() or []
        actions: List[SignalAction] = []
        for state in active:
            vehicle_id = state.vehicle_id
            self._known_vehicles.add(vehicle_id)
            status = self._monitor.status(vehicle_id, lookahead_m=self._lookahead_m)
            upcoming = status.next_intersection
            tls_id = upcoming.tls_id if upcoming is not None else None
            snapshot.emergencies.append(EmergencyEvent(
                event_id=f"emg-{vehicle_id}-{tls_id or 'none'}",
                vehicle_id=vehicle_id,
                timestamp_s=timestamp_s,
                tls_id=tls_id,
                distance_m=round(upcoming.distance_m, 2) if upcoming else None,
                eta_s=round(upcoming.eta_s, 2) if upcoming else None,
                route=list(state.route_edges),
                status="ACTIVE",
                priority=EventPriority.EMERGENCY,
            ))
            action = self._plan_action(vehicle_id, upcoming, coordinator, timestamp_s)
            if action is not None:
                actions.append(action)
                snapshot.events.append(Event(
                    event_id=_new_id("evt"),
                    event_type=EventType.SIGNAL_PRIORITY_REQUEST,
                    source_agent=self.name,
                    timestamp_s=timestamp_s,
                    priority=EventPriority.EMERGENCY,
                    payload={
                        "vehicle_id": vehicle_id,
                        "tls_id": action.tls_id,
                        "eta_s": round(upcoming.eta_s, 2) if upcoming else None,
                        "duration_s": action.duration_s,
                        "reason": action.reason,
                    },
                ))

        if actions or snapshot.emergencies:
            snapshot.recommendations.append(AgentRecommendation(
                recommendation_id=_new_id("rec"),
                agent=self.name,
                decision_type=DecisionType.EMERGENCY_PRIORITY,
                priority=EventPriority.EMERGENCY,
                signal_actions=actions,
                explanation=(
                    f"GreenCorridor tracks {len(snapshot.emergencies)} emergency vehicle(s); "
                    f"{len(actions)} signal priority action(s) proposed."
                ),
                created_at_s=timestamp_s,
                metadata={"active_vehicles": [e.vehicle_id for e in snapshot.emergencies]},
            ))
        return snapshot

    @staticmethod
    def _pick_target_phase(sig: Any) -> Optional[int]:
        """Next phase in program order that serves the ambulance's link."""
        green_indices = list(getattr(sig, "green_phase_indices", []) or [])
        if not green_indices:
            return None
        num = int(getattr(sig, "num_phases", 0) or 0)
        current = int(getattr(sig, "phase_index", 0) or 0)
        for step in range(1, num + 1):
            idx = (current + step) % num
            if idx in green_indices:
                return idx
        return None

    def _plan_action(self, vehicle_id: str, upcoming: Any,
                     coordinator: CoordinatorLike, timestamp_s: float) -> Optional[SignalAction]:
        if upcoming is None or upcoming.signal is None:
            return None
        sig = upcoming.signal
        tls_id = upcoming.tls_id
        eta_s = float(upcoming.eta_s or 0.0)
        remaining = float(sig.phase_remaining_s or 0.0)

        if sig.ambulance_signal == "GREEN":
            margin = remaining - (eta_s + PRIORITY_ETA_BUFFER_S)
            if margin >= 0:
                return None
            duration = min(remaining + PRIORITY_MAX_EXTENSION_S,
                           eta_s + PRIORITY_ETA_BUFFER_S)
            # A hold below the emergency floor is not worth requesting: the light
            # is about to change anyway and the validator would reject it.
            duration = max(duration, PRIORITY_MIN_GREEN_FLOOR_S)
            # Never ask for more than the program allows; if the ETA is beyond
            # that the next cycle re-evaluates and asks again.
            try:
                state = coordinator.signal_state(tls_id)
            except Exception:  # noqa: BLE001
                state = {}
            program_max = float(state.get("max_dur", 0.0) or 0.0)
            if program_max > 0:
                duration = min(duration, program_max)
            if duration <= remaining:
                return None
            return SignalAction(
                action_id=_new_id("act"),
                tls_id=tls_id,
                action_type=ActionType.SET_PHASE_DURATION,
                duration_s=round(duration, 3),
                phase_index=sig.phase_index,
                reason=(f"GreenCorridor: extend the ambulance's green to cover ETA "
                        f"{eta_s:.1f}s (remaining {remaining:.1f}s)"),
                source_agent=AgentName.GREEN_CORRIDOR,
                created_at_s=timestamp_s,
                emergency_vehicle_id=vehicle_id,
            )

        # Conflicting green: shorten it to the program's own minimum floor so
        # the light can advance through its normal yellow/all-red.
        if sig.phase_kind != "GREEN":
            return None
        if self._pick_target_phase(sig) is None:
            return None
        try:
            state = coordinator.signal_state(tls_id)
        except Exception:  # noqa: BLE001
            state = {}
        program_floor = float(state.get("min_dur", 0.0) or 0.0)
        floor = max(program_floor, PRIORITY_MIN_GREEN_FLOOR_S)
        new_remaining = min(remaining, floor)
        if new_remaining >= remaining:
            return None
        return SignalAction(
            action_id=_new_id("act"),
            tls_id=tls_id,
            action_type=ActionType.SET_PHASE_DURATION,
            duration_s=round(new_remaining, 3),
            phase_index=sig.phase_index,
            reason=(f"GreenCorridor: truncate conflicting green to {new_remaining:.1f}s "
                    f"(floor {floor:.1f}s) for {vehicle_id}"),
            source_agent=AgentName.GREEN_CORRIDOR,
            created_at_s=timestamp_s,
            emergency_vehicle_id=vehicle_id,
        )


# --------------------------------------------------------------------------- #
# ParkSense
# --------------------------------------------------------------------------- #
class ParkSenseAdapter(AgentAdapter):
    """Adapter for ``backend.agents.park_sense``."""

    name = AgentName.PARK_SENSE

    def __init__(self, agent: Any = None, zones_path: Optional[str] = None,
                 occupancy_alert_pct: float = 80.0) -> None:
        super().__init__()
        self._agent = agent
        self._zones_path = zones_path
        self._occupancy_alert_pct = occupancy_alert_pct
        self._last_overall_pct: Optional[float] = None
        self._last_status_by_zone: Dict[str, str] = {}

    def initialize(self, coordinator: CoordinatorLike) -> None:
        try:
            if self._agent is None:
                from ..agents.park_sense.agent import ParkSenseAgent
                self._agent = ParkSenseAgent(zones_path=self._zones_path)
            self._mark_ready(f"{len(self._agent.monitor.zones)} parking zones loaded")
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(f"initialisation failed: {exc}")
            raise AgentInitializationError(
                f"ParkSense could not initialise: {exc}") from exc

    def _collect(self, coordinator: CoordinatorLike, timestamp_s: float) -> AgentSnapshot:
        snapshot = AgentSnapshot(agent=self.name)
        counts = coordinator.read_parking_occupancy()
        self._agent.update_occupancy(counts, timestamp_s)

        status = self._agent.get_parking_status()
        zones = [ParkingZoneState(**z) for z in status.get("zones", [])]
        total_capacity = sum(z.capacity for z in zones)
        total_occupied = sum(z.occupied for z in zones)
        overall = round(total_occupied / total_capacity * 100, 1) if total_capacity else 0.0
        snapshot.parking = ParkingState(
            timestamp_s=timestamp_s,
            zones=zones,
            total_capacity=total_capacity,
            total_occupied=total_occupied,
            total_available=max(0, total_capacity - total_occupied),
            overall_occupancy_pct=overall,
        )

        # Occupancy change event.
        if self._last_overall_pct is None or abs(overall - self._last_overall_pct) >= 5.0:
            snapshot.events.append(Event(
                event_id=_new_id("evt"),
                event_type=EventType.PARKING_OCCUPANCY_CHANGED,
                source_agent=self.name,
                timestamp_s=timestamp_s,
                priority=EventPriority.PARKING,
                payload={
                    "overall_occupancy_pct": overall,
                    "total_occupied": total_occupied,
                    "total_available": max(0, total_capacity - total_occupied),
                },
            ))
            self._last_overall_pct = overall

        # Per-zone demand alerts (only on status transitions).
        for zone in zones:
            previous = self._last_status_by_zone.get(zone.zone_id)
            if zone.status in ("FULL", "HIGH") and previous != zone.status:
                snapshot.events.append(Event(
                    event_id=_new_id("evt"),
                    event_type=EventType.PARKING_DEMAND_ALERT,
                    source_agent=self.name,
                    timestamp_s=timestamp_s,
                    priority=EventPriority.PARKING,
                    payload={
                        "zone_id": zone.zone_id,
                        "status": zone.status,
                        "occupancy_pct": zone.occupancy_pct,
                        "available": zone.available,
                    },
                ))
            self._last_status_by_zone[zone.zone_id] = zone.status

        # Advisory recommendations (never touch signals).
        try:
            advice = self._agent.recommend_parking(destination="hospital", top_n=3)
        except Exception as exc:  # noqa: BLE001
            snapshot.notes.append(f"recommend_parking failed: {exc}")
            advice = {}
        by_zone = {z.zone_id: z for z in zones}
        recommendations: List[ParkingRecommendation] = []
        for item in advice.get("recommendations", []) or []:
            zone_id = item.get("zone_id")
            zone_state = by_zone.get(zone_id)
            recommendations.append(ParkingRecommendation(
                zone_id=zone_id,
                name=item.get("name", ""),
                score=round(float(item.get("score", 0.0)), 4),
                available=int(item.get("available_spaces", 0)),
                occupancy_pct=zone_state.occupancy_pct if zone_state else 0.0,
                reason=item.get("reason") or f"demand {item.get('demand_level', 'UNKNOWN')}",
            ))
        if recommendations:
            snapshot.recommendations.append(AgentRecommendation(
                recommendation_id=_new_id("rec"),
                agent=self.name,
                decision_type=DecisionType.PARKING_RECOMMENDATION,
                priority=EventPriority.PARKING,
                parking_recommendations=recommendations,
                explanation=(f"ParkSense recommends {recommendations[0].zone_id} "
                             f"(score {recommendations[0].score:.3f}) for the hospital."),
                created_at_s=timestamp_s,
                metadata={"overall_occupancy_pct": overall},
            ))
        return snapshot


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class AgentRegistry:
    """Registers the three adapters and keeps their availability."""

    def __init__(self) -> None:
        self._adapters: Dict[AgentName, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        if not isinstance(adapter, AgentAdapter):
            raise AgentInitializationError("register() expects an AgentAdapter")
        self._adapters[adapter.name] = adapter

    def get(self, name: AgentName | str) -> AgentAdapter:
        try:
            key = AgentName(name) if not isinstance(name, AgentName) else name
        except ValueError as exc:
            raise AgentNotFoundError(f"Agent '{name}' is not a known agent") from exc
        adapter = self._adapters.get(key)
        if adapter is None:
            raise AgentNotFoundError(f"Agent '{key.value}' is not registered")
        return adapter

    def names(self) -> List[AgentName]:
        return sorted(self._adapters, key=lambda n: n.value)

    def initialize_all(self, coordinator: CoordinatorLike) -> Dict[str, AgentStatus]:
        """Initialise every adapter; one failure never blocks the others."""
        statuses: Dict[str, AgentStatus] = {}
        for name in self.names():
            adapter = self._adapters[name]
            try:
                adapter.initialize(coordinator)
            except AgentInitializationError as exc:
                logger.warning("%s init failed: %s", name.value, exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s init crashed", name.value)
                adapter._mark_failed(str(exc))
            statuses[name.value] = adapter.status()
        return statuses

    def collect_all(self, coordinator: CoordinatorLike,
                    timestamp_s: float) -> List[AgentSnapshot]:
        snapshots: List[AgentSnapshot] = []
        for name in self.names():
            adapter = self._adapters[name]
            if adapter.status().health in (AgentHealth.FAILED, AgentHealth.STOPPED):
                continue
            snapshots.append(adapter.collect(coordinator, timestamp_s))
        return snapshots

    def statuses(self) -> List[AgentStatus]:
        return [self._adapters[name].status() for name in self.names()]

    def available(self) -> List[AgentName]:
        return [name for name in self.names() if self._adapters[name].status().available]

    def shutdown_all(self) -> None:
        for adapter in self._adapters.values():
            try:
                adapter.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("shutdown failed for %s: %s", adapter.name.value, exc)


def build_default_registry(config: Any) -> AgentRegistry:
    """Create the registry with the three real adapters."""
    registry = AgentRegistry()
    if config.enable_traffic_mind:
        registry.register(TrafficMindAdapter())
    if config.enable_green_corridor:
        registry.register(GreenCorridorAdapter())
    if config.enable_park_sense:
        registry.register(ParkSenseAdapter(
            zones_path=str(config.resolved_zones_file()),
            occupancy_alert_pct=config.decisions.parking_alert_occupancy_pct,
        ))
    return registry
