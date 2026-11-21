"""The Traffora-AI central orchestrator.

This is the composition root: it owns the shared state, the agent registry,
the event manager, the decision engine, the safety validator and the single
SUMO session, and it defines the order in which they run.

One control cycle (``run_step``) is:

1. advance SUMO by ``n`` steps (shared session, via the coordinator),
2. let every available agent publish its view of the city (adapters),
3. push agent events into the event manager and fold their payloads into the
   shared state (``process_events``),
4. ask the decision engine for a *deterministic, priority-ordered* list of
   decisions (emergency > traffic > parking),
5. validate every signal action with the independent safety validator,
6. apply only the ``VALID`` actions to SUMO,
7. record the decision, the validation outcome and the measured metrics.

Steps 4-6 are pure Python over plain dictionaries, so they can be unit-tested
without SUMO. No language model participates in this path: the decision engine
is rule-based and every decision carries a human-readable ``explanation``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from .agent_registry import AgentRegistry, AgentSnapshot, build_default_registry
from .config import OrchestratorConfig
from .decision_engine import DecisionEngine
from .event_manager import EventManager
from .exceptions import OrchestratorError
from .safety_validator import SafetyValidator
from .schemas import (
    AgentHealth,
    AgentStatus,
    CityState,
    DecisionType,
    Event,
    EventStatus,
    EventType,
    OrchestratorDecision,
    SignalAction,
    SystemMetrics,
    SystemStatus,
    ValidationResult,
    ValidationStatus,
)
from .shared_state import SharedState
from .simulation_coordinator import SimulationCoordinator

logger = logging.getLogger(__name__)


class TrafforaOrchestrator:
    """Coordinates TrafficMind, GreenCorridor and ParkSense over one SUMO run.

    Parameters
    ----------
    config:
        Tunables and paths. Defaults are repository-relative, never
        machine-specific.
    coordinator, registry, event_manager, decision_engine, safety_validator, state:
        Optional injections used by the tests to run the orchestrator with
        mocked SUMO connections and fake agents. In production all of them are
        built from ``config``.

    Example
    -------
    >>> orch = TrafforaOrchestrator()
    >>> orch.initialize()          # starts SUMO, initialises the agents
    >>> orch.start()               # marks the system running
    >>> for _ in range(120):       # 120 simulated seconds
    ...     orch.run_step()
    >>> orch.shutdown()
    """

    def __init__(
        self,
        config: Optional[OrchestratorConfig] = None,
        *,
        coordinator: Optional[SimulationCoordinator] = None,
        registry: Optional[AgentRegistry] = None,
        event_manager: Optional[EventManager] = None,
        decision_engine: Optional[DecisionEngine] = None,
        safety_validator: Optional[SafetyValidator] = None,
        state: Optional[SharedState] = None,
    ) -> None:
        self.config = config or OrchestratorConfig()
        self.state = state or SharedState(decision_history_size=self.config.decisions.decision_history_size)
        self.coordinator = coordinator or SimulationCoordinator(self.config)
        self.registry = registry or build_default_registry(self.config)
        self.events = event_manager or EventManager(
            dedupe_window_s=self.config.event_dedupe_window_s,
            max_pending=self.config.max_pending_events,
        )
        self.decisions = decision_engine or DecisionEngine(self.config.decisions)
        self.validator = safety_validator or SafetyValidator(self.config.safety)

        self._initialized = False
        self._running = False
        self._steps = 0
        self._last_collect_s: Optional[float] = None
        self._last_decision_count = 0
        self._emergency_first_seen: Dict[str, float] = {}
        self._emergency_first_action: Dict[str, float] = {}
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def initialize(self) -> None:
        """Start SUMO, then initialise every enabled agent against it.

        A failing agent does **not** abort start-up: its status becomes
        ``FAILED`` and the remaining agents keep working, so a single broken
        agent cannot take the city down.
        """
        if self._initialized:
            logger.debug("initialize() called twice; ignoring.")
            return

        logger.info("Initialising Traffora-AI orchestrator (scenario: %s)", self.config.resolved_sumocfg())
        self.coordinator.start()

        known = set(self.coordinator.known_tls_ids)
        self.validator.set_known_tls(known)
        logger.info("Safety validator knows %d traffic lights.", len(known))

        statuses = self.registry.initialize_all(self.coordinator)
        for status in statuses.values():
            self.state.set_agent_status(status)
            logger.info("Agent %s: %s (%s)", status.agent.value, status.health.value, status.message)

        self.state.set_simulation_time(self.coordinator.current_time)
        self._initialized = True

    def start(self) -> None:
        """Mark the orchestrator running. Calls ``initialize()`` if needed."""
        if not self._initialized:
            self.initialize()
        self._running = True
        self._started_at = time.perf_counter()
        logger.info("Orchestrator started.")

    @property
    def is_running(self) -> bool:
        return self._running and self.coordinator.is_running

    def shutdown(self) -> None:
        """Stop the orchestrator and close the single SUMO session."""
        logger.info("Shutting down orchestrator after %d steps.", self._steps)
        self._running = False
        try:
            self.registry.shutdown_all()
        finally:
            self.coordinator.close()
            self._initialized = False
            for status in self.registry.statuses():
                if status.health is not AgentHealth.FAILED:
                    self.state.set_agent_status(
                        status.model_copy(update={"health": AgentHealth.STOPPED, "available": False})
                    )

    def __enter__(self) -> "TrafforaOrchestrator":
        self.initialize()
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ #
    # One control cycle
    # ------------------------------------------------------------------ #
    def run_step(self, n: int = 1) -> List[OrchestratorDecision]:
        """Advance the simulation and run one full orchestration cycle.

        Returns the decisions taken this cycle (never empty: a ``NO_ACTION``
        decision is recorded when nothing needs doing).
        """
        if not self._initialized:
            raise OrchestratorError("Orchestrator is not initialised; call initialize() first.")
        if not self._running:
            raise OrchestratorError("Orchestrator is not running; call start() first.")

        cycle_start = time.perf_counter()
        now = self.coordinator.step(n)
        self._steps += n
        self.state.set_simulation_time(now)

        snapshots = self._collect(now)
        self.process_events(snapshots)

        city_state = self.state.snapshot()
        recommendations = [rec for snap in snapshots for rec in snap.recommendations]

        decisions = self.decisions.decide(city_state, recommendations, timestamp_s=now)
        decisions = self._validate_and_apply(decisions, now)

        for decision in decisions:
            self.state.record_decision(decision)
        self._last_decision_count = len(decisions)

        self._record_metrics(now)
        latency_ms = (time.perf_counter() - cycle_start) * 1000.0
        self.state.record_latency(latency_ms)
        return decisions

    def _collect(self, now: float) -> List[AgentSnapshot]:
        """Sample every available agent (throttled by ``decision_interval_s``)."""
        if self._last_collect_s is not None and (now - self._last_collect_s) < self.config.decision_interval_s:
            return []
        self._last_collect_s = now
        snapshots = self.registry.collect_all(self.coordinator, now)
        for status in self.registry.statuses():
            self.state.set_agent_status(status)
        return snapshots

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def process_events(self, snapshots: Optional[Sequence[AgentSnapshot]] = None) -> List[Event]:
        """Publish agent events, fold them into shared state, then drain.

        ``snapshots`` may be supplied by ``run_step``; when it is ``None`` the
        method only drains whatever is already queued (useful for the API).
        """
        if snapshots:
            self._publish_snapshots(snapshots)

        processed: List[Event] = []
        for event in self.events.drain():
            try:
                self._handle_event(event)
                self.events.mark_processed(event, EventStatus.PROCESSED)
                processed.append(event)
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the cycle
                logger.warning("Failed to handle event %s (%s): %s", event.event_id, event.event_type.value, exc)
                self.events.mark_processed(event, EventStatus.FAILED, error=str(exc))
        return processed

    def _publish_snapshots(self, snapshots: Sequence[AgentSnapshot]) -> None:
        for snap in snapshots:
            for note in snap.notes:
                self.state.add_note(note)

            if snap.traffic is not None:
                self.state.update_traffic(snap.traffic)
            if snap.parking is not None:
                self.state.update_parking(snap.parking)
            for emergency in snap.emergencies:
                self.state.upsert_emergency(emergency)
            for vehicle_id in snap.arrived_emergencies:
                self.state.remove_emergency(vehicle_id)
                self._emergency_first_seen.pop(vehicle_id, None)
                self._emergency_first_action.pop(vehicle_id, None)

            for event in snap.events:
                self.events.try_publish(event)

    def _handle_event(self, event: Event) -> None:
        """Apply an event's payload to the shared state."""
        payload = event.payload
        if event.event_type is EventType.EMERGENCY_DETECTED:
            vehicle_id = payload.get("vehicle_id")
            if vehicle_id:
                self._emergency_first_seen.setdefault(vehicle_id, event.timestamp_s)
        elif event.event_type is EventType.EMERGENCY_ROUTE_UPDATE:
            logger.debug("Route update for %s: %s", payload.get("vehicle_id"), payload.get("route"))
        elif event.event_type is EventType.SIGNAL_PRIORITY_REQUEST:
            logger.info(
                "Priority request from %s for %s at %s",
                event.source_agent.value,
                payload.get("vehicle_id"),
                payload.get("tls_id"),
            )
        elif event.event_type is EventType.CONGESTION_DETECTED:
            logger.info(
                "Congestion at %s (level=%s, queue=%.0fm)",
                payload.get("junction_id") or payload.get("edge_id"),
                payload.get("level"),
                float(payload.get("queue_length_m") or 0.0),
            )
        elif event.event_type is EventType.CONGESTION_PREDICTED:
            logger.info(
                "Congestion predicted at %s in %.0fs",
                payload.get("junction_id") or payload.get("edge_id"),
                float(payload.get("horizon_s") or 0.0),
            )
        elif event.event_type is EventType.PARKING_OCCUPANCY_CHANGED:
            logger.debug("Parking %s occupancy %s", payload.get("zone_id"), payload.get("occupancy_pct"))
        elif event.event_type is EventType.PARKING_DEMAND_ALERT:
            logger.info(
                "Parking demand alert for %s (level=%s)",
                payload.get("zone_id"),
                payload.get("level"),
            )

    # ------------------------------------------------------------------ #
    # Validation + application
    # ------------------------------------------------------------------ #
    def _validate_and_apply(
        self, decisions: Sequence[OrchestratorDecision], now: float
    ) -> List[OrchestratorDecision]:
        """Validate every signal action and apply only the safe ones.

        Parking advice and ``NO_ACTION`` decisions carry no signal writes, so
        they pass through untouched.
        """
        signal_actions: List[SignalAction] = []
        for decision in decisions:
            signal_actions.extend(decision.signal_actions)

        if not signal_actions:
            return list(decisions)

        # Baseline mode: the agents still run and still produce recommendations
        # (so their output stays observable), but nothing is written to SUMO.
        # This is what makes a like-for-like fixed-time comparison possible.
        if not self.config.enable_signal_control:
            updated: List[OrchestratorDecision] = []
            for decision in decisions:
                if not decision.signal_actions:
                    updated.append(decision)
                    continue
                updated.append(decision.model_copy(update={
                    "applied": False,
                    "rejected_reason": "signal control disabled (fixed-time baseline)",
                    "validation": ValidationResult(
                        valid=False, status=ValidationStatus.DEFERRED,
                        reasons=["signal control disabled (fixed-time baseline)"],
                        checked_at_s=now),
                }))
            return updated

        states = self.coordinator.signal_states({a.tls_id for a in signal_actions})
        emergency_tls = {
            a.tls_id
            for d in decisions
            if d.decision_type is DecisionType.EMERGENCY_PRIORITY
            for a in d.signal_actions
        }
        accepted, results = self.validator.validate_batch(
            signal_actions, states, emergency_tls=emergency_tls, timestamp_s=now
        )
        by_id: Dict[str, ValidationResult] = {a.action_id: r for a, r in zip(signal_actions, results)}

        if accepted:
            applied_records = self.coordinator.apply(accepted, by_id)
            # Response time is measured against the cycle clock rather than a
            # key inside the coordinator's return value: the orchestrator must
            # not depend on the shape of a backend-specific record.
            if applied_records:
                for vehicle_id in self._emergency_first_seen:
                    self._emergency_first_action.setdefault(vehicle_id, now)
            response_times = [
                self._emergency_first_action[v] - self._emergency_first_seen[v]
                for v in self._emergency_first_action
                if v in self._emergency_first_seen
            ]
            if response_times:
                self.state.set_metric(
                    "emergency_priority_response_time_s", sum(response_times) / len(response_times)
                )

        updated: List[OrchestratorDecision] = []
        for decision in decisions:
            if not decision.signal_actions:
                updated.append(decision)
                continue

            decision_results = [by_id[a.action_id] for a in decision.signal_actions]
            rejected = [r for r in decision_results if r.status is not ValidationStatus.VALID]
            if rejected:
                reasons = "; ".join(reason for r in rejected for reason in r.reasons) or "rejected by safety validator"
                updated.append(
                    decision.model_copy(
                        update={
                            "applied": False,
                            "rejected_reason": reasons,
                            "validation": rejected[0],
                            "explanation": f"{decision.explanation} | REJECTED: {reasons}",
                        }
                    )
                )
            else:
                updated.append(
                    decision.model_copy(update={"applied": True, "validation": decision_results[0]})
                )
        return updated

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #
    def _record_metrics(self, now: float) -> None:
        measured = self.coordinator.collect_metrics()
        if not measured:
            return
        self.state.set_metric("simulation_time_s", now)
        self.state.set_metric("steps", int(self._steps))
        self.state.set_metric("vehicles_departed", int(measured.get("vehicles_departed") or 0))
        self.state.set_metric("vehicles_arrived", int(measured.get("vehicles_arrived") or 0))
        self.state.record_metric_sample(
            waiting_time_s=measured.get("avg_waiting_time_s"),
            queue_length_m=measured.get("avg_queue_length_veh"),
            travel_time_s=measured.get("avg_travel_time_s"),
        )
        if measured.get("emergency_travel_time_s"):
            self.state.set_metric("emergency_vehicle_travel_time_s", measured["emergency_travel_time_s"])

        parking = self.state.get_parking()
        if parking is not None and parking.zones:
            total_capacity = sum(z.capacity for z in parking.zones) or 1
            total_occupied = sum(z.occupied for z in parking.zones)
            self.state.set_metric("parking_occupancy_pct", 100.0 * total_occupied / total_capacity)

    # ------------------------------------------------------------------ #
    # Public read interface
    # ------------------------------------------------------------------ #
    def get_city_state(self) -> CityState:
        """Latest traffic, emergency, parking, agent and decision snapshot."""
        return self.state.snapshot()

    def get_system_status(self) -> SystemStatus:
        """Health of the whole system, for ``GET /status``."""
        return SystemStatus(
            running=self.is_running,
            simulation_time_s=self.state.simulation_time_s,
            config={
                "sumocfg": str(self.config.resolved_sumocfg()),
                "seed": self.config.seed,
                "step_length": self.config.step_length,
                "end_time": self.config.end_time,
                "label": self.config.label,
                "signal_control_enabled": self.config.enable_signal_control,
            },
            agents=self.registry.statuses(),
            pending_events=self.events.pending_count,
            decisions=len(self.state.get_decisions()),
            metrics=self.state.metrics(),
        )

    # ------------------------------------------------------------------ #
    # FastAPI-ready service methods
    # ------------------------------------------------------------------ #
    def start_simulation(self) -> Dict[str, Any]:
        """``POST /simulation/start`` - initialise SUMO and the agents."""
        self.initialize()
        self.start()
        return {"started": True, **self.coordinator.snapshot_summary()}

    def stop_simulation(self) -> Dict[str, Any]:
        """``POST /simulation/stop`` - close the SUMO session."""
        self.shutdown()
        return {"started": False, "running": False}

    def get_agent_statuses(self) -> List[AgentStatus]:
        """``GET /agents`` - one entry per registered agent."""
        return self.registry.statuses()

    def get_latest_decision(self) -> Optional[OrchestratorDecision]:
        """``GET /decisions/latest`` - the most recent decision."""
        return self.state.get_latest_decision()

    def get_decisions(self, limit: int = 50) -> List[OrchestratorDecision]:
        """``GET /decisions`` - decision history, newest first."""
        return self.state.get_decisions(limit=limit)

    def get_emergency_events(self) -> List[Dict[str, Any]]:
        """``GET /emergency`` - active emergency vehicles plus their events."""
        active = [e.model_dump(mode="json") for e in self.state.get_emergency_vehicles()]
        events = [
            e.model_dump(mode="json")
            for e in self.events.history()
            if e.event_type
            in (
                EventType.EMERGENCY_DETECTED,
                EventType.EMERGENCY_ROUTE_UPDATE,
                EventType.SIGNAL_PRIORITY_REQUEST,
            )
        ]
        return active + events

    def get_traffic_analytics(self) -> Dict[str, Any]:
        """``GET /analytics/traffic`` - measured traffic figures."""
        traffic = self.state.get_traffic()
        metrics = self.state.metrics()
        return {
            "simulation_time_s": self.state.simulation_time_s,
            "traffic": traffic.model_dump(mode="json") if traffic else None,
            "average_waiting_time_s": metrics.average_waiting_time_s,
            "average_queue_length": metrics.average_queue_length_m,
            "average_travel_time_s": metrics.average_travel_time_s,
            "vehicles_departed": metrics.vehicles_departed,
            "vehicles_arrived": metrics.vehicles_arrived,
            "congestion_events": self.events.stats().get("processed", 0),
        }

    def get_parking_analytics(self) -> Dict[str, Any]:
        """``GET /analytics/parking`` - measured parking figures."""
        parking = self.state.get_parking()
        metrics = self.state.metrics()
        return {
            "parking": parking.model_dump(mode="json") if parking else None,
            "occupancy_pct": metrics.parking_occupancy_pct,
            "search_time_s": metrics.parking_search_time_s,
        }

    def get_simulation_metrics(self) -> SystemMetrics:
        """``GET /metrics`` - every measured metric, nothing estimated."""
        return self.state.metrics()

    def get_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """``GET /events`` - recent events, newest last."""
        return [e.model_dump(mode="json") for e in self.events.history(limit=limit)]
