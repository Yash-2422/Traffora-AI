"""Single shared SUMO session for the whole Traffora-AI stack.

Every agent in this project can talk to SUMO on its own. If the orchestrator
did the same, three TraCI connections would drive three *different*
simulations and the city state would be inconsistent. This module therefore
owns exactly **one** ``SimulationInterface`` (the TrafficMind implementation,
reused rather than re-implemented) and exposes it to the agents:

* ``simulation`` - the ``SimulationInterface`` object TrafficMind attaches to.
* ``connection`` - the raw, labelled TraCI connection object that GreenCorridor's
  read-only ``EmergencyDetector`` / ``IntersectionMonitor`` accept.

The coordinator is deliberately the *only* module in ``backend/orchestrator``
that imports TraCI. Business logic (decision engine, safety validator) works on
plain dictionaries, so it stays testable without SUMO installed.

Write safety
------------
``apply()`` is the only method that writes to SUMO and it accepts only
``SignalAction`` objects whose ``ValidationResult`` is ``VALID``. The single
TraCI command it ever issues is ``trafficlight.setPhaseDuration`` - it never
changes the phase index and never sets a raw red/yellow/green state string.
That keeps the network's own conflict-free phase sequence intact, which is why
simultaneous conflicting greens are structurally impossible.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from backend.agents.traffic_mind.config import SimulationConfig
from backend.agents.traffic_mind.simulation_interface import SimulationInterface

from .config import OrchestratorConfig
from .exceptions import (
    SafetyViolationError,
    SimulationError,
    SimulationNotRunningError,
    SimulationStartError,
)
from .schemas import SignalAction, ValidationResult, ValidationStatus

logger = logging.getLogger(__name__)

# TraCI signal characters, see https://sumo.dlr.de/docs/Simulation/Traffic_Lights.html
_GREEN_CHARS = frozenset("Gg")
_YELLOW_CHARS = frozenset("Yy")
_RED_CHARS = frozenset("r")


def _phase_kind(state: str) -> str:
    """Classify a TraCI signal state string as GREEN / YELLOW / RED / OTHER.

    ``G``/``g`` are green (``g`` is a permissive green), ``Y``/``y`` yellow,
    ``r`` red. A state that mixes green and yellow is reported as YELLOW
    because the junction is already clearing.
    """
    chars = set(state or "")
    if not chars:
        return "UNKNOWN"
    if chars & _YELLOW_CHARS:
        return "YELLOW"
    if chars & _GREEN_CHARS:
        return "GREEN"
    if chars <= _RED_CHARS:
        return "RED"
    return "OTHER"


class SimulationCoordinator:
    """Owns one SUMO/TraCI session and mediates all reads and writes.

    Parameters
    ----------
    config:
        Orchestrator configuration; ``sumocfg``, ``label``, ``seed``,
        ``end_time`` and ``step_length`` are forwarded to
        :class:`~backend.agents.traffic_mind.simulation_interface.SimulationInterface`.
    """

    def __init__(self, config: Optional[OrchestratorConfig] = None) -> None:
        self.config = config or OrchestratorConfig()
        self._sim = SimulationInterface(
            SimulationConfig(
                sumocfg=self.config.resolved_sumocfg(),
                label=self.config.label,
                seed=self.config.seed,
                step_length=self.config.step_length,
                end_time=self.config.end_time,
                demand_scale=self.config.demand_scale,
                gui=self.config.gui,
                gui_delay_ms=self.config.gui_delay_ms,
                connect_retries=self.config.connect_retries,
            )
        )
        self._known_tls: Set[str] = set()
        # tls_id -> (phase_index, sim_time_when_that_phase_started)
        self._phase_start: Dict[str, tuple[int, float]] = {}
        self._departure_times: Dict[str, float] = {}
        self._arrival_times: Dict[str, float] = {}
        self._applied_actions: int = 0
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._sim.is_running and not self._sim.has_ended

    @property
    def has_ended(self) -> bool:
        return self._sim.has_ended

    @property
    def simulation(self) -> SimulationInterface:
        """The shared ``SimulationInterface`` handed to TrafficMind."""
        return self._sim

    @property
    def connection(self) -> Any:
        """The raw labelled TraCI connection, for GreenCorridor's read-only tools.

        ``SimulationInterface`` keeps the connection private, and we must not
        modify the agent. We therefore look for a public attribute first and
        fall back to the documented private one.
        """
        conn = getattr(self._sim, "connection", None)
        if conn is not None:
            return conn
        return getattr(self._sim, "_conn", None)

    def start(self) -> None:
        """Start SUMO and open the one shared TraCI connection."""
        try:
            self._sim.start()
        except Exception as exc:  # noqa: BLE001 - normalised to our own error
            raise SimulationStartError(
                f"Could not start the SUMO simulation: {exc}",
                details={"sumocfg": str(self.config.resolved_sumocfg())},
            ) from exc

        conn = self.connection
        if conn is None:  # pragma: no cover - defensive
            raise SimulationStartError("SUMO started but no TraCI connection is available.")

        try:
            self._known_tls = set(conn.trafficlight.getIDList())
        except Exception as exc:  # noqa: BLE001
            raise SimulationStartError(f"Could not read traffic lights from SUMO: {exc}") from exc

        self._activate_program(conn)

        self._started_at = time.perf_counter()
        logger.info(
            "SUMO session '%s' ready: %d traffic lights, %d parking areas",
            self.config.label,
            len(self._known_tls),
            len(self._parking_area_ids()),
        )

    def _activate_program(self, conn: Any) -> None:
        """Activate the authored signal program, once, at startup.

        ``signals.add.xml`` ships the scenario's plans under their own program
        ID (SUMO refuses a duplicate ID for the network's default program).
        Switching to it is a one-off simulation setup step: it selects a
        complete, conflict-free program, it does not set a phase or a state.
        """
        program_id = getattr(self.config, "signal_program_id", "") or ""
        if not program_id:
            return
        switched = 0
        for tls_id in sorted(self._known_tls):
            try:
                available = {logic.programID
                             for logic in conn.trafficlight.getAllProgramLogics(tls_id)}
            except Exception:  # noqa: BLE001 - older TraCI versions
                available = set()
            try:
                if available and program_id not in available:
                    continue
                conn.trafficlight.setProgram(tls_id, program_id)
                switched += 1
            except Exception as exc:  # noqa: BLE001 - keep SUMO's default program
                logger.warning("Could not activate program %s on %s: %s",
                               program_id, tls_id, exc)
        if switched:
            logger.info("Activated signal program '%s' on %d traffic lights.",
                        program_id, switched)
        else:
            logger.warning("Signal program '%s' was not found; using SUMO defaults.",
                           program_id)

    def close(self) -> None:
        """Close the shared connection. Safe to call more than once."""
        self._sim.close()
        self._known_tls = set()
        logger.info(
            "SUMO session '%s' closed after %d applied actions.",
            self.config.label,
            self._applied_actions,
        )

    # ------------------------------------------------------------------ #
    # Stepping
    # ------------------------------------------------------------------ #
    @property
    def current_time(self) -> float:
        if not self._sim.is_running:
            raise SimulationNotRunningError("Simulation is not running.")
        return self._sim.current_time

    def step(self, n: int = 1) -> float:
        """Advance ``n`` simulation steps and return the new simulation time."""
        if not self.is_running:
            raise SimulationNotRunningError("Simulation is not running; call start() first.")
        try:
            now = self._sim.step(n)
        except Exception as exc:  # noqa: BLE001
            raise SimulationError(f"Simulation step failed: {exc}") from exc
        self._track_trips()
        self._update_phase_tracking()
        return now

    def _update_phase_tracking(self) -> None:
        """Record when each signal entered its current phase.

        ``setPhaseDuration`` sets the *remaining* duration of the running phase,
        so the safety validator needs to know how much green has already been
        served. That is measured here, one TraCI read per signal per step.
        """
        conn = self.connection
        if conn is None:
            return
        try:
            now = float(conn.simulation.getTime())
            for tls_id in self._known_tls:
                phase = int(conn.trafficlight.getPhase(tls_id))
                previous = self._phase_start.get(tls_id)
                if previous is None or previous[0] != phase:
                    self._phase_start[tls_id] = (phase, now)
        except Exception as exc:  # noqa: BLE001 - tracking must never break a step
            logger.debug("Phase tracking failed: %s", exc)

    def _track_trips(self) -> None:
        """Remember departure/arrival times so travel times can be measured."""
        conn = self.connection
        if conn is None:  # pragma: no cover - defensive
            return
        try:
            now = float(conn.simulation.getTime())
            for vid in conn.simulation.getDepartedIDList():
                self._departure_times.setdefault(vid, now)
            for vid in conn.simulation.getArrivedIDList():
                self._arrival_times.setdefault(vid, now)
                self._departure_times.setdefault(vid, now)
        except Exception as exc:  # noqa: BLE001 - never break a step for metrics
            logger.debug("Trip tracking failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Traffic lights (read)
    # ------------------------------------------------------------------ #
    @property
    def known_tls_ids(self) -> Set[str]:
        """IDs of the traffic lights present in the running network."""
        if not self._known_tls and self._sim.is_running:
            conn = self.connection
            if conn is not None:
                self._known_tls = set(conn.trafficlight.getIDList())
        return set(self._known_tls)

    def _program_limits(self, tls_id: str) -> Dict[str, float]:
        """Min/max phase duration declared by the *active* program, if any."""
        conn = self.connection
        limits: Dict[str, float] = {"min_dur": 0.0, "max_dur": 0.0}
        if conn is None:
            return limits
        try:
            program_id = conn.trafficlight.getProgram(tls_id)
            logics = conn.trafficlight.getAllProgramLogics(tls_id)
            logic = next((lg for lg in logics if lg.programID == program_id), logics[0])
            phase_index = int(conn.trafficlight.getPhase(tls_id))
            if 0 <= phase_index < len(logic.phases):
                phase = logic.phases[phase_index]
                limits["min_dur"] = float(getattr(phase, "minDur", 0.0) or 0.0)
                limits["max_dur"] = float(getattr(phase, "maxDur", 0.0) or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read program limits for %s: %s", tls_id, exc)
        return limits

    def signal_state(self, tls_id: str) -> Optional[Dict[str, Any]]:
        """Snapshot of one signal, shaped for :class:`SafetyValidator`.

        Returns ``None`` when the junction is unknown, so the validator can
        defer instead of guessing.
        """
        conn = self.connection
        if conn is None or not self.is_running:
            return None
        try:
            now = float(conn.simulation.getTime())
            phase = int(conn.trafficlight.getPhase(tls_id))
            state = str(conn.trafficlight.getRedYellowGreenState(tls_id))
            remaining = max(0.0, float(conn.trafficlight.getNextSwitch(tls_id)) - now)
            logics = conn.trafficlight.getAllProgramLogics(tls_id)
            num_phases = len(logics[0].phases) if logics else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read signal state for %s: %s", tls_id, exc)
            return None

        limits = self._program_limits(tls_id)
        tracked = self._phase_start.get(tls_id)
        elapsed = max(0.0, now - tracked[1]) if tracked and tracked[0] == phase else 0.0
        return {
            "tls_id": tls_id,
            "phase": phase,
            "elapsed_s": elapsed,
            "num_phases": num_phases,
            "remaining_s": remaining,
            "phase_kind": _phase_kind(state),
            "state": state,
            "min_dur": limits["min_dur"],
            "max_dur": limits["max_dur"],
            "sim_time_s": now,
        }

    def signal_states(self, tls_ids: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
        """``signal_state`` for several junctions, skipping unknown ones."""
        ids = list(tls_ids) if tls_ids is not None else sorted(self.known_tls_ids)
        states: Dict[str, Dict[str, Any]] = {}
        for tls_id in ids:
            state = self.signal_state(tls_id)
            if state is not None:
                states[tls_id] = state
        return states

    # ------------------------------------------------------------------ #
    # Parking (read)
    # ------------------------------------------------------------------ #
    def _parking_area_ids(self) -> List[str]:
        conn = self.connection
        if conn is None:
            return []
        try:
            return sorted(conn.parkingarea.getIDList())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not list parking areas: %s", exc)
            return []

    def read_parking_occupancy(self) -> Dict[str, int]:
        """Live occupancy (parked vehicle count) of every SUMO ``parkingArea``.

        This is real simulation state read over TraCI, not a synthetic value.
        """
        conn = self.connection
        if conn is None or not self.is_running:
            return {}
        occupancy: Dict[str, int] = {}
        for area_id in self._parking_area_ids():
            try:
                occupancy[area_id] = int(conn.parkingarea.getVehicleCount(area_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not read occupancy of %s: %s", area_id, exc)
        return occupancy

    # ------------------------------------------------------------------ #
    # Writes - only validated signal durations
    # ------------------------------------------------------------------ #
    def apply(
        self,
        actions: Sequence[SignalAction],
        validations: Mapping[str, ValidationResult],
    ) -> List[Dict[str, Any]]:
        """Apply validated ``SignalAction`` objects to SUMO.

        Every action must carry a ``VALID`` :class:`ValidationResult`; anything
        else raises :class:`SafetyViolationError` and **nothing** is written.
        The check is done for the whole batch first, so a single unsafe action
        cannot leave the network half-updated.

        Returns one record per applied action (used for logging and metrics).
        """
        if not actions:
            return []
        if not self.is_running:
            raise SimulationNotRunningError("Cannot apply actions: simulation is not running.")

        unsafe: List[str] = []
        for action in actions:
            result = validations.get(action.action_id)
            if result is None or result.status is not ValidationStatus.VALID:
                unsafe.append(action.action_id)
        if unsafe:
            raise SafetyViolationError(
                "Refusing to write unvalidated signal actions to SUMO.",
                details={"action_ids": unsafe},
            )

        conn = self.connection
        applied: List[Dict[str, Any]] = []
        for action in actions:
            try:
                conn.trafficlight.setPhaseDuration(action.tls_id, float(action.duration_s))
            except Exception as exc:  # noqa: BLE001
                raise SimulationError(
                    f"SUMO rejected setPhaseDuration for {action.tls_id}: {exc}",
                    details={"action_id": action.action_id},
                ) from exc
            self._applied_actions += 1
            applied.append(
                {
                    "action_id": action.action_id,
                    "tls_id": action.tls_id,
                    "duration_s": float(action.duration_s),
                    "phase_index": action.phase_index,
                    "sim_time_s": float(conn.simulation.getTime()),
                }
            )
            logger.info(
                "Applied %s on %s: phase %s held for %.1fs (source=%s)",
                action.action_type.value,
                action.tls_id,
                action.phase_index,
                action.duration_s,
                action.source_agent.value,
            )
        return applied

    @property
    def applied_action_count(self) -> int:
        return self._applied_actions

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #
    def collect_metrics(self) -> Dict[str, float]:
        """Measure network-level metrics from the live simulation.

        All values are measured over the vehicles currently in the network,
        except travel times, which are computed from completed trips.
        """
        conn = self.connection
        if conn is None or not self.is_running:
            return {}

        metrics: Dict[str, float] = {"sim_time_s": float(conn.simulation.getTime())}
        try:
            vehicle_ids = list(conn.vehicle.getIDList())
            metrics["vehicles_running"] = float(len(vehicle_ids))
            # ``getDepartedNumber``/``getArrivedNumber`` only count the last step,
            # so the cumulative figures come from our own trip tracking.
            metrics["vehicles_departed"] = float(len(self._departure_times))
            metrics["vehicles_arrived"] = float(len(self._arrival_times))

            if vehicle_ids:
                waiting = [float(conn.vehicle.getWaitingTime(v)) for v in vehicle_ids]
                metrics["avg_waiting_time_s"] = sum(waiting) / len(waiting)
                speeds = [float(conn.vehicle.getSpeed(v)) for v in vehicle_ids]
                metrics["avg_speed_mps"] = sum(speeds) / len(speeds)
            else:
                metrics["avg_waiting_time_s"] = 0.0
                metrics["avg_speed_mps"] = 0.0

            edges = [e for e in conn.edge.getIDList() if not e.startswith(":")]
            if edges:
                halting = [int(conn.edge.getLastStepHaltingNumber(e)) for e in edges]
                metrics["avg_queue_length_veh"] = sum(halting) / len(halting)
                metrics["total_halting"] = float(sum(halting))
            else:
                metrics["avg_queue_length_veh"] = 0.0
                metrics["total_halting"] = 0.0
        except Exception as exc:  # noqa: BLE001
            logger.debug("Metric collection partially failed: %s", exc)

        travel_times = [
            self._arrival_times[v] - self._departure_times[v]
            for v in self._arrival_times
            if v in self._departure_times and self._arrival_times[v] >= self._departure_times[v]
        ]
        metrics["avg_travel_time_s"] = sum(travel_times) / len(travel_times) if travel_times else 0.0
        metrics["completed_trips"] = float(len(travel_times))

        prefixes = tuple(self.config.emergency_vehicle_prefixes)
        emergency_times = [
            self._arrival_times[v] - self._departure_times[v]
            for v in self._arrival_times
            if v.startswith(prefixes) and v in self._departure_times
        ]
        metrics["emergency_travel_time_s"] = (
            sum(emergency_times) / len(emergency_times) if emergency_times else 0.0
        )
        metrics["emergency_vehicles_completed"] = float(len(emergency_times))
        metrics["applied_signal_actions"] = float(self._applied_actions)
        return metrics

    def emergency_vehicles(self) -> List[str]:
        """IDs of emergency vehicles currently in the network (by ID prefix)."""
        conn = self.connection
        if conn is None or not self.is_running:
            return []
        prefixes = tuple(self.config.emergency_vehicle_prefixes)
        try:
            return [v for v in conn.vehicle.getIDList() if v.startswith(prefixes)]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not list emergency vehicles: %s", exc)
            return []

    def snapshot_summary(self) -> Dict[str, Any]:
        """Small JSON-serialisable description of the session (for status APIs)."""
        return {
            "running": self.is_running,
            "ended": self.has_ended,
            "label": self.config.label,
            "sumocfg": str(self.config.resolved_sumocfg()),
            "sim_time_s": self._sim.current_time if self._sim.is_running else None,
            "traffic_lights": len(self.known_tls_ids),
            "parking_areas": len(self._parking_area_ids()),
            "applied_signal_actions": self._applied_actions,
        }
