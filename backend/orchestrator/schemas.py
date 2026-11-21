"""Common Pydantic schemas shared by the orchestrator and its adapters.

The three agents use different internal models (TrafficMind: Pydantic v2,
GreenCorridor/ParkSense: dataclasses with ``to_dict()``). Rather than forcing
any agent to change, the orchestrator defines one canonical vocabulary here and
the adapters convert agent output into these models explicitly. That keeps the
agents independently testable and avoids incompatible duplicate schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp (never a naive datetime)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class EventPriority(IntEnum):
    """Processing order. Lower value = handled first."""

    EMERGENCY = 1
    TRAFFIC = 2
    PARKING = 3

    @property
    def label(self) -> str:
        return self.name


class EventType(str, Enum):
    CONGESTION_DETECTED = "CONGESTION_DETECTED"
    CONGESTION_PREDICTED = "CONGESTION_PREDICTED"
    EMERGENCY_DETECTED = "EMERGENCY_DETECTED"
    EMERGENCY_ROUTE_UPDATE = "EMERGENCY_ROUTE_UPDATE"
    SIGNAL_PRIORITY_REQUEST = "SIGNAL_PRIORITY_REQUEST"
    PARKING_OCCUPANCY_CHANGED = "PARKING_OCCUPANCY_CHANGED"
    PARKING_DEMAND_ALERT = "PARKING_DEMAND_ALERT"


class EventStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class AgentName(str, Enum):
    TRAFFIC_MIND = "traffic_mind"
    GREEN_CORRIDOR = "green_corridor"
    PARK_SENSE = "park_sense"


class AgentHealth(str, Enum):
    UNKNOWN = "UNKNOWN"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class DecisionType(str, Enum):
    EMERGENCY_PRIORITY = "EMERGENCY_PRIORITY"
    TRAFFIC_OPTIMIZATION = "TRAFFIC_OPTIMIZATION"
    PARKING_RECOMMENDATION = "PARKING_RECOMMENDATION"
    NO_ACTION = "NO_ACTION"


class ActionType(str, Enum):
    """The only signal writes the orchestrator is allowed to perform.

    ``SET_PHASE_DURATION`` is the single TraCI write used (it never changes the
    phase index or the red/yellow/green state string). ``RESTORE_SIGNAL``
    returns a light to its program-defined duration after an emergency pass.
    """

    SET_PHASE_DURATION = "SET_PHASE_DURATION"
    RESTORE_SIGNAL = "RESTORE_SIGNAL"
    PARKING_ADVICE = "PARKING_ADVICE"
    NONE = "NONE"


class ValidationStatus(str, Enum):
    VALID = "VALID"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


# --------------------------------------------------------------------------- #
# Traffic
# --------------------------------------------------------------------------- #
class TrafficState(BaseModel):
    """Canonical traffic snapshot consumed by the decision engine."""

    model_config = ConfigDict(extra="ignore")

    timestamp_s: float = Field(ge=0)
    vehicle_count: int = Field(default=0, ge=0)
    average_speed_mps: float = Field(default=0.0, ge=0)
    average_waiting_time_s: float = Field(default=0.0, ge=0)
    average_queue_length_m: float = Field(default=0.0, ge=0)
    congestion_level: str = "LOW"
    congested_junctions: List[str] = Field(default_factory=list)
    junction_levels: Dict[str, str] = Field(default_factory=dict)
    source: str = AgentName.TRAFFIC_MIND.value

    @field_validator("congestion_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


# --------------------------------------------------------------------------- #
# Emergency
# --------------------------------------------------------------------------- #
class EmergencyEvent(BaseModel):
    """One tracked emergency vehicle and its next signalised intersection."""

    model_config = ConfigDict(extra="ignore")

    event_id: str
    vehicle_id: str
    timestamp_s: float = Field(ge=0)
    tls_id: Optional[str] = None
    distance_m: Optional[float] = Field(default=None, ge=0)
    eta_s: Optional[float] = Field(default=None, ge=0)
    route: List[str] = Field(default_factory=list)
    status: str = "ACTIVE"
    priority: EventPriority = EventPriority.EMERGENCY


# --------------------------------------------------------------------------- #
# Parking
# --------------------------------------------------------------------------- #
class ParkingZoneState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    zone_id: str
    name: str = ""
    capacity: int = Field(ge=0)
    occupied: int = Field(ge=0)
    available: int = Field(ge=0)
    occupancy_pct: float = Field(ge=0, le=100)
    status: str = "UNKNOWN"


class ParkingState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp_s: float = Field(ge=0)
    zones: List[ParkingZoneState] = Field(default_factory=list)
    total_capacity: int = Field(default=0, ge=0)
    total_occupied: int = Field(default=0, ge=0)
    total_available: int = Field(default=0, ge=0)
    overall_occupancy_pct: float = Field(default=0.0, ge=0, le=100)


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
class AgentStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent: AgentName
    health: AgentHealth = AgentHealth.UNKNOWN
    available: bool = False
    message: str = ""
    last_update_s: Optional[float] = None
    latency_ms: Optional[float] = Field(default=None, ge=0)
    details: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Actions and recommendations
# --------------------------------------------------------------------------- #
class SignalAction(BaseModel):
    """A proposed signal write. Never applied before safety validation."""

    model_config = ConfigDict(extra="ignore")

    action_id: str
    tls_id: str
    action_type: ActionType = ActionType.SET_PHASE_DURATION
    duration_s: float = Field(gt=0)
    phase_index: Optional[int] = Field(default=None, ge=0)
    reason: str = ""
    source_agent: AgentName = AgentName.TRAFFIC_MIND
    created_at_s: float = Field(default=0.0, ge=0)
    # Set by the decision engine when an emergency request owns the light.
    emergency_vehicle_id: Optional[str] = None


class ParkingRecommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    zone_id: str
    name: str = ""
    score: float = 0.0
    available: int = Field(default=0, ge=0)
    occupancy_pct: float = Field(default=0.0, ge=0, le=100)
    reason: str = ""


class AgentRecommendation(BaseModel):
    """Normalised output of any agent, ready for the decision engine."""

    model_config = ConfigDict(extra="ignore")

    recommendation_id: str
    agent: AgentName
    decision_type: DecisionType
    priority: EventPriority
    signal_actions: List[SignalAction] = Field(default_factory=list)
    parking_recommendations: List[ParkingRecommendation] = Field(default_factory=list)
    explanation: str = ""
    created_at_s: float = Field(default=0.0, ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Validation and decisions
# --------------------------------------------------------------------------- #
class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    valid: bool
    status: ValidationStatus
    reasons: List[str] = Field(default_factory=list)
    checked_at_s: float = Field(default=0.0, ge=0)


class OrchestratorDecision(BaseModel):
    """One explainable decision, with the validation outcome attached."""

    model_config = ConfigDict(extra="ignore")

    decision_id: str
    timestamp_s: float = Field(ge=0)
    decision_type: DecisionType
    priority: EventPriority
    source_agent: Optional[AgentName] = None
    signal_actions: List[SignalAction] = Field(default_factory=list)
    parking_recommendations: List[ParkingRecommendation] = Field(default_factory=list)
    explanation: str = ""
    validation: Optional[ValidationResult] = None
    applied: bool = False
    rejected_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
class Event(BaseModel):
    """An event published by an agent and processed by the orchestrator."""

    model_config = ConfigDict(extra="ignore")

    event_id: str
    event_type: EventType
    source_agent: AgentName
    timestamp_s: float = Field(ge=0)
    priority: EventPriority
    payload: Dict[str, Any] = Field(default_factory=dict)
    status: EventStatus = EventStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: Optional[datetime] = None
    error: Optional[str] = None
    dedupe_key: Optional[str] = None

    def stable_key(self) -> str:
        """Deterministic identity used for duplicate suppression."""
        if self.dedupe_key:
            return self.dedupe_key
        parts = [self.event_type.value, self.source_agent.value]
        for field in ("junction_id", "tls_id", "vehicle_id", "zone_id", "alert_id"):
            value = self.payload.get(field)
            if value is not None:
                parts.append(f"{field}={value}")
        return "|".join(parts)


# --------------------------------------------------------------------------- #
# City state, metrics, status
# --------------------------------------------------------------------------- #
class CityState(BaseModel):
    """Everything the dashboard needs for one instant."""

    model_config = ConfigDict(extra="ignore")

    timestamp_s: float = Field(ge=0)
    traffic: Optional[TrafficState] = None
    emergency_vehicles: List[EmergencyEvent] = Field(default_factory=list)
    parking: Optional[ParkingState] = None
    agents: List[AgentStatus] = Field(default_factory=list)
    latest_decision: Optional[OrchestratorDecision] = None


class SystemMetrics(BaseModel):
    """Measured metrics only. Nothing here is estimated or hardcoded."""

    model_config = ConfigDict(extra="ignore")

    simulation_time_s: float = 0.0
    steps: int = 0
    vehicles_departed: int = 0
    vehicles_arrived: int = 0
    average_waiting_time_s: Optional[float] = None
    average_queue_length_m: Optional[float] = None
    average_travel_time_s: Optional[float] = None
    emergency_vehicle_travel_time_s: Optional[float] = None
    emergency_priority_response_time_s: Optional[float] = None
    parking_occupancy_pct: Optional[float] = None
    parking_search_time_s: Optional[float] = None
    accepted_decisions: int = 0
    rejected_decisions: int = 0
    orchestrator_processing_latency_ms: Optional[float] = None
    agent_availability_pct: Optional[float] = None
    notes: List[str] = Field(default_factory=list)


class SystemStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    running: bool
    simulation_time_s: float
    config: Dict[str, Any] = Field(default_factory=dict)
    agents: List[AgentStatus] = Field(default_factory=list)
    pending_events: int = 0
    decisions: int = 0
    metrics: SystemMetrics = Field(default_factory=SystemMetrics)
