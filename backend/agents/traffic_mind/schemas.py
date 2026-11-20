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

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    """Base model: reject unknown fields so typos in input data are caught."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

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
    length_m: float = Field(gt=0)
    lane_count: int = Field(ge=1)
    speed_limit_mps: float = Field(gt=0)
    vehicle_count: int = Field(ge=0)
    density_veh_per_km: float = Field(ge=0)          # vehicles per km of road (all lanes)
    density_veh_per_km_lane: float = Field(ge=0)     # vehicles per km per lane
    occupancy_pct: float = Field(ge=0, le=100)
    queue_length_veh: int = Field(ge=0)              # stopped vehicles
    queue_length_m: float = Field(ge=0)              # queue length measured from the stop line
    mean_waiting_time_s: float = Field(ge=0)         # average over vehicles currently on the road
    mean_speed_mps: float = Field(ge=0)
    speed_ratio: float = Field(ge=0, le=1)           # mean speed / speed limit
    flow_veh_per_hour: float = Field(ge=0)           # vehicles entering the road (rolling window)

    @model_validator(mode="after")
    def _check_consistency(self) -> "EdgeMetrics":
        if self.queue_length_veh > self.vehicle_count:
            raise ValueError(
                f"edge {self.edge_id}: queue_length_veh ({self.queue_length_veh}) "
                f"exceeds vehicle_count ({self.vehicle_count})"
            )
        if self.queue_length_m > self.length_m + 1e-6:
            raise ValueError(
                f"edge {self.edge_id}: queue_length_m ({self.queue_length_m}) "
                f"exceeds the road length ({self.length_m})"
            )
        return self


class JunctionMetrics(_Model):
    junction_id: str
    incoming_edges: List[str]
    vehicle_count: int = Field(ge=0)                 # vehicles on the incoming roads
    queue_length_veh: int = Field(ge=0)
    max_queue_length_m: float = Field(ge=0)
    mean_waiting_time_s: float = Field(ge=0)         # vehicle-weighted average
    max_occupancy_pct: float = Field(ge=0, le=100)
    flow_veh_per_hour: float = Field(ge=0)           # sum over incoming roads
    current_phase: Optional[int] = Field(default=None, ge=0)
    phase_remaining_s: Optional[float] = Field(default=None, ge=0)


class NetworkMetrics(_Model):
    total_vehicles: int = Field(ge=0)
    halting_vehicles: int = Field(ge=0)
    mean_waiting_time_s: float = Field(ge=0)
    mean_speed_ratio: float = Field(ge=0, le=1)


class TrafficSnapshot(_Model):
    """The result of ``get_traffic_status()``."""

    status: str = "ok"
    timestamp_s: float = Field(ge=0)
    network: NetworkMetrics
    junctions: List[JunctionMetrics]
    edges: List[EdgeMetrics]


# --------------------------------------------------------------------------- #
# Congestion detection
# --------------------------------------------------------------------------- #
class CongestionLevel(str, Enum):
    """Congestion class. Serialises to "LOW", "MEDIUM" or "HIGH"."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return _LEVEL_RANK[self]


_LEVEL_RANK = {CongestionLevel.LOW: 0, CongestionLevel.MEDIUM: 1, CongestionLevel.HIGH: 2}


class CongestionFactors(_Model):
    """The four measured signals behind a congestion score, each scaled to 0..1."""

    occupancy: float = Field(ge=0, le=1)
    queue: float = Field(ge=0, le=1)
    waiting: float = Field(ge=0, le=1)
    speed_drop: float = Field(ge=0, le=1)


class EdgeCongestion(_Model):
    edge_id: str
    junction_id: Optional[str] = None
    score: float = Field(ge=0, le=1)
    raw_level: CongestionLevel          # level of the current score
    level: CongestionLevel              # after the persistence rule (use this one)
    factors: CongestionFactors
    dominant_factor: str
    vehicle_count: int
    queue_length_veh: int
    queue_length_m: float


class JunctionCongestion(_Model):
    junction_id: str
    score: float = Field(ge=0, le=1)    # score of the worst approach road
    raw_level: CongestionLevel
    level: CongestionLevel
    since_s: float                      # simulation time the current level started
    worst_edge_id: Optional[str] = None
    dominant_factor: str
    factors: CongestionFactors          # factors of the worst approach road
    vehicle_count: int
    queue_length_veh: int
    max_queue_length_m: float
    mean_waiting_time_s: float
    requires_attention: bool
    attention_reason: Optional[str] = None


class CongestionAlert(_Model):
    alert_id: str                       # stays the same while the alert stays active
    entity_type: Literal["junction", "road"]
    entity_id: str
    level: CongestionLevel
    severity: Literal["warning", "critical"]
    score: float
    since_s: float
    is_new: bool                        # True only in the update where the level was reached
    message: str


class AttentionJunction(_Model):
    rank: int = Field(ge=1)             # 1 = needs attention most
    junction_id: str
    level: CongestionLevel
    score: float
    reason: str
    worst_edge_id: Optional[str] = None
    max_queue_length_m: float
    mean_waiting_time_s: float


class CongestionSummary(_Model):
    junctions_low: int
    junctions_medium: int
    junctions_high: int
    roads_low: int
    roads_medium: int
    roads_high: int


class CongestionReport(_Model):
    """The result of ``detect_congestion()``."""

    status: str = "ok"
    timestamp_s: float
    summary: CongestionSummary
    junctions: List[JunctionCongestion]
    edges: List[EdgeCongestion]
    alerts: List[CongestionAlert]
    attention_junctions: List[AttentionJunction]


# --------------------------------------------------------------------------- #
# Traffic prediction
# --------------------------------------------------------------------------- #
class ModelInfo(_Model):
    """Describes the trained model behind a prediction."""

    name: str
    horizon_s: int
    feature_count: int
    trained_at: Optional[str] = None
    training_rows: Optional[int] = None
    training_runs: Optional[int] = None
    test_macro_f1: Optional[float] = None


class JunctionPrediction(_Model):
    junction_id: str
    current_level: CongestionLevel
    current_score: float = Field(ge=0, le=1)
    predicted_level: CongestionLevel            # level expected ``horizon_s`` seconds ahead
    probabilities: Dict[str, float]             # LOW / MEDIUM / HIGH, adds up to 1
    confidence: float = Field(ge=0, le=1)       # probability of the predicted level
    expected_change: Literal["worsening", "steady", "improving"]
    method: Literal["ml", "persistence"]

    @model_validator(mode="after")
    def _check_probabilities(self) -> "JunctionPrediction":
        if set(self.probabilities) != {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("probabilities must have the keys LOW, MEDIUM and HIGH")
        if any(not 0.0 <= p <= 1.0 for p in self.probabilities.values()):
            raise ValueError("probabilities must be between 0 and 1")
        if abs(sum(self.probabilities.values()) - 1.0) > 1e-3:
            raise ValueError("probabilities must add up to 1")
        return self


class EarlyWarning(_Model):
    junction_id: str
    current_level: CongestionLevel
    predicted_level: CongestionLevel
    confidence: float = Field(ge=0, le=1)
    message: str


class PredictionReport(_Model):
    """The result of ``predict_congestion()``."""

    status: str = "ok"
    timestamp_s: float = Field(ge=0)
    horizon_s: int = Field(gt=0)
    method: Literal["ml", "persistence"]
    model: Optional[ModelInfo] = None
    history_available_s: float = Field(ge=0)
    predictions: List[JunctionPrediction]
    early_warnings: List[EarlyWarning]
    notes: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Signal programs and recommendations
# --------------------------------------------------------------------------- #
class RawSignalPhase(_Model):
    state: str                                   # SUMO signal state, one character per link
    duration_s: float = Field(gt=0)


class RawSignalProgram(_Model):
    """A junction's fixed signal program as read from the simulation."""

    junction_id: str
    phases: List[RawSignalPhase]
    link_edges: List[Optional[str]]              # incoming road of each signal link (by index)


class SignalPhase(_Model):
    index: int = Field(ge=0)
    state: str
    duration_s: float = Field(gt=0)
    kind: Literal["green", "yellow", "all_red"]


class SignalStage(_Model):
    """One green phase and the yellow / all-red phases that follow it."""

    stage_id: int = Field(ge=1)
    green_phase: int = Field(ge=0)               # index of the green phase in ``phases``
    clearance_phases: List[int]                  # indexes of the yellow / all-red phases after it
    edges: List[str]                             # roads that get green in this stage
    green_s: float = Field(gt=0)
    yellow_s: float = Field(ge=0)
    all_red_s: float = Field(ge=0)


class SignalProgram(_Model):
    junction_id: str
    phases: List[SignalPhase]
    stages: List[SignalStage]
    cycle_s: float = Field(gt=0)


class StagePlan(_Model):
    stage_id: int
    edges: List[str]
    demand: float = Field(ge=0, le=1)            # worst approach congestion score of the stage
    current_green_s: float
    recommended_green_s: float
    change_s: float


class ImmediateAction(_Model):
    """What to do with the signal phase that is running right now."""

    action: Literal["none", "extend_green", "end_green_early", "in_clearance"]
    extend_s: float = Field(default=0.0, ge=0)
    reason: str = ""


class SafetyCheck(_Model):
    validated: bool
    rules_checked: List[str]


class JunctionSignalRecommendation(_Model):
    junction_id: str
    level: CongestionLevel
    predicted_level: Optional[CongestionLevel] = None
    priority: Literal["none", "low", "medium", "high"]
    action: Literal["keep", "rebalance"]
    stages: List[StagePlan]
    yellow_s: float                              # unchanged clearance times
    all_red_s: float
    cycle_s: float                               # cycle length with the recommended greens
    current_phase: Optional[int] = None
    phase_elapsed_s: Optional[float] = None
    immediate: ImmediateAction
    reason: str
    safety: SafetyCheck


class SignalSummary(_Model):
    junctions_rebalance: int
    junctions_keep: int
    immediate_actions: int


class SignalRecommendationReport(_Model):
    """The result of ``generate_signal_recommendation()``. Advice only: nothing is executed."""

    status: str = "ok"
    mode: Literal["recommend_only"] = "recommend_only"
    timestamp_s: float = Field(ge=0)
    summary: SignalSummary
    recommendations: List[JunctionSignalRecommendation]
    notes: List[str] = Field(default_factory=list)