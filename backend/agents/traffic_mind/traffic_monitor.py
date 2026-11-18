"""Traffic monitoring: turns raw simulation readings into traffic metrics.

This module is pure Python (no SUMO / TraCI import). It consumes
``RawObservation`` objects - or equivalent dicts - and produces a
``TrafficSnapshot`` with, per road and per signalised junction:

* vehicle count and density
* road occupancy
* queue length (vehicles and metres)
* average waiting time
* mean speed relative to the speed limit
* traffic flow (vehicles/hour entering, over a rolling window)

Typical use with the simulation::

    with SimulationInterface() as sim:
        monitor = TrafficMonitor.from_source(sim)
        sim.step(60)
        snapshot = monitor.sample(sim)
        print(snapshot.to_dict())
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Protocol, Set, Tuple, Union

from pydantic import ValidationError

from .config import MonitorConfig
from .exceptions import InvalidTrafficDataError
from .schemas import (
    EdgeInfo,
    EdgeMetrics,
    JunctionInfo,
    JunctionMetrics,
    NetworkMetrics,
    NetworkTopology,
    RawEdgeObservation,
    RawObservation,
    TrafficSnapshot,
)

logger = logging.getLogger(__name__)

ObservationInput = Union[RawObservation, Dict[str, Any]]


class ObservationSource(Protocol):
    """Anything that can describe the network and report observations
    (``SimulationInterface`` does; tests use fakes)."""

    def read_topology(self) -> NetworkTopology: ...

    def read_observation(self) -> RawObservation: ...


class _FlowTracker:
    """Estimates how many vehicles entered a road in a rolling time window.

    A vehicle counts as "entered" when its id is on the road now but was not
    at the previous sample. Samples must be spaced closer than the time a
    vehicle needs to cross the road (true for the default 5 s sampling).
    """

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._previous_ids: Optional[Set[str]] = None
        self._previous_time: Optional[float] = None
        # (interval start, interval end, vehicles that entered in the interval)
        self._intervals: Deque[Tuple[float, float, int]] = deque()

    def update(self, time_s: float, vehicle_ids: Set[str]) -> float:
        """Record a sample and return the flow in vehicles/hour."""
        if self._previous_ids is not None and self._previous_time is not None:
            entered = len(vehicle_ids - self._previous_ids)
            if time_s > self._previous_time:
                self._intervals.append((self._previous_time, time_s, entered))
        self._previous_ids = set(vehicle_ids)
        self._previous_time = time_s

        while self._intervals and self._intervals[0][1] <= time_s - self.window_s:
            self._intervals.popleft()
        if not self._intervals:
            return 0.0
        span = self._intervals[-1][1] - self._intervals[0][0]
        if span <= 0:
            return 0.0
        total = sum(entered for _, _, entered in self._intervals)
        return total / span * 3600.0


class TrafficMonitor:
    """Computes traffic metrics from raw observations.

    Args:
        topology: the static road network (from ``source.read_topology()``).
        config: monitor settings (flow window, history length).
    """

    def __init__(
        self,
        topology: Union[NetworkTopology, Dict[str, Any]],
        config: Optional[MonitorConfig] = None,
    ) -> None:
        self.config = config or MonitorConfig()
        try:
            self.topology = (
                topology if isinstance(topology, NetworkTopology)
                else NetworkTopology.model_validate(topology)
            )
        except ValidationError as exc:
            raise InvalidTrafficDataError(f"Invalid network topology: {exc}") from exc

        self._edges: Dict[str, EdgeInfo] = {e.edge_id: e for e in self.topology.edges}
        self._junctions: List[JunctionInfo] = list(self.topology.junctions)
        self._flows: Dict[str, _FlowTracker] = {}
        self._history: Deque[TrafficSnapshot] = deque(maxlen=self.config.history_size)
        self._last_time: Optional[float] = None

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def from_source(
        cls, source: ObservationSource, config: Optional[MonitorConfig] = None
    ) -> "TrafficMonitor":
        """Build a monitor from the topology reported by a simulation."""
        return cls(source.read_topology(), config)

    def sample(self, source: ObservationSource) -> TrafficSnapshot:
        """Read one observation from ``source`` and process it."""
        return self.update(source.read_observation())

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    @property
    def latest(self) -> Optional[TrafficSnapshot]:
        return self._history[-1] if self._history else None

    @property
    def history(self) -> List[TrafficSnapshot]:
        return list(self._history)

    def reset(self) -> None:
        """Forget history and flow state (e.g. after a simulation restart)."""
        self._flows.clear()
        self._history.clear()
        self._last_time = None

    # ------------------------------------------------------------------ #
    # Processing
    # ------------------------------------------------------------------ #
    def update(self, observation: ObservationInput) -> TrafficSnapshot:
        """Process one observation and return the resulting snapshot.

        Raises:
            InvalidTrafficDataError: the observation is malformed, refers to
                unknown roads, or goes back in time.
        """
        obs = self._validate(observation)

        if self._last_time is not None and obs.time_s < self._last_time:
            logger.warning(
                "Observation time went backwards (%.1f -> %.1f); resetting monitor state.",
                self._last_time, obs.time_s,
            )
            self.reset()
        self._last_time = obs.time_s

        edge_metrics: Dict[str, EdgeMetrics] = {}
        for raw in obs.edges:
            edge_metrics[raw.edge_id] = self._edge_metrics(raw, obs.time_s)

        signal_by_junction = {s.junction_id: s for s in obs.signals}
        junction_metrics = [
            self._junction_metrics(j, edge_metrics, signal_by_junction.get(j.junction_id))
            for j in self._junctions
        ]

        snapshot = TrafficSnapshot(
            timestamp_s=obs.time_s,
            network=self._network_metrics(obs, edge_metrics),
            junctions=junction_metrics,
            edges=list(edge_metrics.values()),
        )
        self._history.append(snapshot)
        return snapshot

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _validate(self, observation: ObservationInput) -> RawObservation:
        try:
            obs = (
                observation if isinstance(observation, RawObservation)
                else RawObservation.model_validate(observation)
            )
        except ValidationError as exc:
            raise InvalidTrafficDataError(f"Invalid traffic observation: {exc}") from exc

        unknown = sorted({e.edge_id for e in obs.edges} - set(self._edges))
        if unknown:
            raise InvalidTrafficDataError(
                f"Observation refers to roads that are not in the network: {unknown[:5]}"
            )
        seen = set()
        for edge in obs.edges:
            if edge.edge_id in seen:
                raise InvalidTrafficDataError(f"Duplicate observation for road {edge.edge_id}")
            seen.add(edge.edge_id)
        known_junctions = {j.junction_id for j in self._junctions}
        bad_signals = sorted({s.junction_id for s in obs.signals} - known_junctions)
        if bad_signals:
            raise InvalidTrafficDataError(
                f"Observation refers to junctions that are not in the network: {bad_signals[:5]}"
            )
        return obs

    def _edge_metrics(self, raw: RawEdgeObservation, time_s: float) -> EdgeMetrics:
        info = self._edges[raw.edge_id]
        length_km = info.length_m / 1000.0
        density = raw.vehicle_count / length_km
        mean_wait = raw.waiting_time_sum_s / raw.vehicle_count if raw.vehicle_count else 0.0
        speed_ratio = min(1.0, raw.mean_speed_mps / info.speed_limit_mps)

        tracker = self._flows.get(raw.edge_id)
        if tracker is None:
            tracker = self._flows[raw.edge_id] = _FlowTracker(self.config.flow_window_s)
        flow = tracker.update(time_s, set(raw.vehicle_ids))

        return EdgeMetrics(
            edge_id=raw.edge_id,
            junction_id=info.junction_id,
            length_m=info.length_m,
            lane_count=info.lane_count,
            speed_limit_mps=info.speed_limit_mps,
            vehicle_count=raw.vehicle_count,
            density_veh_per_km=round(density, 3),
            density_veh_per_km_lane=round(density / info.lane_count, 3),
            occupancy_pct=round(raw.occupancy_pct, 3),
            queue_length_veh=raw.halting_count,
            queue_length_m=round(raw.queue_length_m, 2),
            mean_waiting_time_s=round(mean_wait, 2),
            mean_speed_mps=round(raw.mean_speed_mps, 3),
            speed_ratio=round(speed_ratio, 4),
            flow_veh_per_hour=round(flow, 1),
        )

    def _junction_metrics(
        self,
        junction: JunctionInfo,
        edge_metrics: Dict[str, EdgeMetrics],
        signal: Optional[Any],
    ) -> JunctionMetrics:
        incoming = [edge_metrics[e] for e in junction.incoming_edges if e in edge_metrics]
        vehicles = sum(e.vehicle_count for e in incoming)
        weighted_wait = sum(e.mean_waiting_time_s * e.vehicle_count for e in incoming)
        return JunctionMetrics(
            junction_id=junction.junction_id,
            incoming_edges=list(junction.incoming_edges),
            vehicle_count=vehicles,
            queue_length_veh=sum(e.queue_length_veh for e in incoming),
            max_queue_length_m=max((e.queue_length_m for e in incoming), default=0.0),
            mean_waiting_time_s=round(weighted_wait / vehicles, 2) if vehicles else 0.0,
            max_occupancy_pct=max((e.occupancy_pct for e in incoming), default=0.0),
            flow_veh_per_hour=round(sum(e.flow_veh_per_hour for e in incoming), 1),
            current_phase=signal.phase_index if signal else None,
            phase_remaining_s=signal.phase_remaining_s if signal else None,
        )

    @staticmethod
    def _network_metrics(
        obs: RawObservation, edge_metrics: Dict[str, EdgeMetrics]
    ) -> NetworkMetrics:
        edges = list(edge_metrics.values())
        on_roads = sum(e.vehicle_count for e in edges)
        weighted_wait = sum(e.mean_waiting_time_s * e.vehicle_count for e in edges)
        ratio_sum = sum(e.speed_ratio * e.vehicle_count for e in edges)
        return NetworkMetrics(
            total_vehicles=obs.total_vehicles,
            halting_vehicles=sum(e.queue_length_veh for e in edges),
            mean_waiting_time_s=round(weighted_wait / on_roads, 2) if on_roads else 0.0,
            mean_speed_ratio=round(ratio_sum / on_roads, 4) if on_roads else 1.0,
        )