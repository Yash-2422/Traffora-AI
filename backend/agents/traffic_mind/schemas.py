"""Pydantic data models for the TrafficMind agent.

Three groups of models:

* **Topology** (``NetworkTopology``): the static road network, read once.
* **Raw observations** (``RawObservation``): what the simulation reports at one
  moment. The monitor accepts these (or equivalent dicts), so it can be tested
  and reused without SUMO.
* **Metrics** (``TrafficSnapshot`` and friends): the agent's output. Always
  JSON-serializable via ``.to_dict()``.

Later steps add congestion, prediction and signal models to this module.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    """Base model: reject unknown fields so typos in input data are caught."""

    model_config = ConfigDict(extra="forbid")

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable dict."""
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #
class EdgeInfo(_Model):
    edge_id: str
    length_m: float = Field(gt=0)
    lane_count: int = Field(ge=1)
    speed_limit_mps: float = Field(gt=0)
    # Signalised junction this road leads into (None for border exits).
    junction_id: Optional[str] = None


class JunctionInfo(_Model):
    junction_id: str
    incoming_edges: List[str]
    phase_count: int = Field(ge=0, default=0)


class NetworkTopology(_Model):
    edges: List[EdgeInfo]
    junctions: List[JunctionInfo]

    @model_validator(mode="after")
    def _check_references(self) -> "NetworkTopology":
        edge_ids = [e.edge_id for e in self.edges]
        if len(set(edge_ids)) != len(edge_ids):
            raise ValueError("duplicate edge ids in topology")
        known = set(edge_ids)
        for junction in self.junctions:
            missing = [e for e in junction.incoming_edges if e not in known]
            if missing:
                raise ValueError(
                    f"junction {junction.junction_id} references unknown edges: {missing}"
                )
        return self


# --------------------------------------------------------------------------- #
# Raw observations (input to the monitor)
# --------------------------------------------------------------------------- #
class RawEdgeObservation(_Model):
    edge_id: str
    vehicle_ids: List[str] = Field(default_factory=list)
    vehicle_count: int = Field(ge=0)
    halting_count: int = Field(ge=0)          # vehicles stopped (< 0.1 m/s)
    occupancy_pct: float = Field(ge=0, le=100)
    mean_speed_mps: float = Field(ge=0)       # SUMO reports the speed limit when empty
    waiting_time_sum_s: float = Field(ge=0)   # summed over all vehicles on the road
    queue_length_m: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> "RawEdgeObservation":
        if self.halting_count > self.vehicle_count:
            raise ValueError(
                f"edge {self.edge_id}: halting_count ({self.halting_count}) "
                f"exceeds vehicle_count ({self.vehicle_count})"
            )
        if self.vehicle_ids and len(self.vehicle_ids) != self.vehicle_count:
            raise ValueError(
                f"edge {self.edge_id}: vehicle_ids length does not match vehicle_count"
            )
        return self


class RawSignalObservation(_Model):
    junction_id: str
    phase_index: int = Field(ge=0)
    phase_remaining_s: float = Field(ge=0)


class RawObservation(_Model):
    time_s: float = Field(ge=0)
    total_vehicles: int = Field(ge=0)
    edges: List[RawEdgeObservation]
    signals: List[RawSignalObservation] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Metrics (output of the monitor)
# --------------------------------------------------------------------------- #
class EdgeMetrics(_Model):
    edge_id: str
    junction_id: Optional[str] = None
    length_m: float
    lane_count: int
    speed_limit_mps: float
    vehicle_count: int
    density_veh_per_km: float          # vehicles per km of road (all lanes)
    density_veh_per_km_lane: float     # vehicles per km per lane
    occupancy_pct: float
    queue_length_veh: int              # stopped vehicles
    queue_length_m: float              # length of the queue measured from the stop line
    mean_waiting_time_s: float         # average over vehicles currently on the road
    mean_speed_mps: float
    speed_ratio: float                 # mean speed / speed limit, 0..1
    flow_veh_per_hour: float           # vehicles entering the road (rolling window)


class JunctionMetrics(_Model):
    junction_id: str
    incoming_edges: List[str]
    vehicle_count: int                 # vehicles on the incoming roads
    queue_length_veh: int
    max_queue_length_m: float
    mean_waiting_time_s: float         # vehicle-weighted average
    max_occupancy_pct: float
    flow_veh_per_hour: float           # sum over incoming roads
    current_phase: Optional[int] = None
    phase_remaining_s: Optional[float] = None


class NetworkMetrics(_Model):
    total_vehicles: int
    halting_vehicles: int
    mean_waiting_time_s: float
    mean_speed_ratio: float


class TrafficSnapshot(_Model):
    """The result of ``get_traffic_status()``."""

    status: str = "ok"
    timestamp_s: float
    network: NetworkMetrics
    junctions: List[JunctionMetrics]
    edges: List[EdgeMetrics]