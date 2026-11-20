"""Structured, JSON-serialisable data types shared by all GreenCorridor components."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class EmergencyStatus(str, Enum):
    IDLE = "IDLE"
    DETECTED = "DETECTED"
    ROUTE_PLANNED = "ROUTE_PLANNED"   # set by the route planner (Step 5)
    EN_ROUTE = "EN_ROUTE"
    ARRIVED = "ARRIVED"
    ROUTE_BLOCKED = "ROUTE_BLOCKED"   # set by the route planner (Step 5)
    ERROR = "ERROR"


class Movement(str, Enum):
    STOPPED = "STOPPED"
    ACCELERATING = "ACCELERATING"
    DECELERATING = "DECELERATING"
    CRUISING = "CRUISING"


def to_jsonable(value: Any) -> Any:
    """Recursively convert enums/tuples/floats into plain JSON types."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, float):
        return round(value, 2)
    return value


@dataclass
class EmergencyVehicleState:
    """Snapshot of one emergency vehicle at one simulation step."""
    vehicle_id: str
    vehicle_type: str
    vehicle_class: str
    sim_time_s: float
    status: EmergencyStatus
    movement: Movement
    x: float
    y: float
    heading_deg: float
    speed_mps: float
    speed_kmh: float
    acceleration_mps2: float
    road_id: str                        # current edge, or SUMO internal id ':J_0' inside a junction
    lane_id: str
    lane_position_m: float
    on_junction: bool
    route_edges: List[str]
    route_index: int
    destination_edge: Optional[str]
    distance_travelled_m: float
    remaining_distance_m: Optional[float]   # driving distance to the END of the destination edge
    waiting_time_s: float                   # current continuous stop time (SUMO definition)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ok(data: Any) -> Dict[str, Any]:
    """Success envelope used by every public GreenCorridor function."""
    return {"success": True, "data": to_jsonable(data), "error": None, "timestamp": _now_iso()}


def fail(code: str, message: str) -> Dict[str, Any]:
    """Failure envelope. Public functions never raise; they return this."""
    return {"success": False, "data": None, "error": {"code": code, "message": message},
            "timestamp": _now_iso()}


@dataclass
class RouteIntersection:
    """A signalised junction the route passes through."""
    tls_id: str
    approach_edge: str               # edge the vehicle is on when it reaches the junction
    exit_edge: str                   # next edge after the junction
    route_index: int                 # index of approach_edge within the route
    distance_from_start_m: float     # approximate (edge lengths scaled to SUMO's route length)
    eta_from_start_s: float          # estimated, excludes waiting at signals
    x: Optional[float] = None
    y: Optional[float] = None


@dataclass
class EmergencyRoute:
    origin_edge: str
    destination_edge: str
    vehicle_type: str
    edges: List[str]
    num_edges: int
    distance_m: float                # SUMO route length (includes junction crossings)
    estimated_travel_time_s: float   # live-traffic estimate; excludes signal waiting (measured in Step 8)
    free_flow_travel_time_s: float   # same edges at the speed limit
    congestion_factor: float         # estimated / free_flow
    intersections: List[RouteIntersection]
    num_intersections: int
    routing_mode: str
    computed_at_sim_time_s: float
    reason: Optional[str] = None     # None for first plan, else e.g. "ROUTE_BLOCKED"
    applied_to_vehicle: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))


@dataclass
class RouteCheck:
    """Result of monitoring a vehicle's current route against present conditions."""
    vehicle_id: str
    sim_time_s: float
    needs_recalculation: bool
    reason: Optional[str]            # None | "ROUTE_BLOCKED" | "FASTER_ALTERNATIVE"
    blocked_edges: List[str]
    alternative_available: bool
    current_remaining_eta_s: float
    best_alternative_eta_s: Optional[float]
    estimated_gain_s: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))


@dataclass
class SignalSnapshot:
    """State of one traffic light at one simulation step, from the ambulance's point of view."""
    tls_id: str
    program_id: str
    phase_index: int
    num_phases: int
    phase_state: str                      # one character per controlled link, e.g. "GggrrrGGg"
    phase_kind: str                       # "GREEN" | "CLEARANCE" (any yellow) | "ALL_RED"
    phase_remaining_s: float
    next_switch_time_s: float
    ambulance_link_index: Optional[int]   # index of the ambulance's movement inside phase_state
    ambulance_signal: Optional[str]       # "GREEN" | "YELLOW" | "RED" | "RED_YELLOW" | "OFF" | "UNKNOWN"
    green_phase_indices: List[int]        # phases of the program in which the ambulance's link is green


@dataclass
class UpcomingIntersection:
    order: int                            # 1 = next intersection
    tls_id: str
    approach_edge: str
    exit_edge: str
    route_index: int
    distance_m: float                     # to the stop line along the route (SUMO getNextTLS)
    eta_s: float                          # live-traffic estimate to the stop line, excludes signal waiting
    eta_at_current_speed_s: Optional[float]   # distance / current speed (None when almost stopped)
    x: Optional[float]
    y: Optional[float]
    signal: Optional[SignalSnapshot]


@dataclass
class IntersectionCrossing:
    """A signalised intersection the vehicle has completed crossing."""
    tls_id: str
    approach_edge: str
    exit_edge: str
    entered_time_s: Optional[float]       # first step seen inside the junction (None if the step skipped it)
    crossed_time_s: float                 # first step seen on the exit edge
    wait_time_s: float                    # time stopped while this was the next signal within the wait zone
    speed_mps: float                      # speed when first seen on the exit edge


@dataclass
class IntersectionStatus:
    vehicle_id: str
    sim_time_s: float
    vehicle_active: bool                  # False once the vehicle has left the simulation
    on_junction: bool
    current_intersection: Optional[str]   # tls id while the vehicle is inside a signalised junction
    next_intersection: Optional[UpcomingIntersection]
    upcoming: List[UpcomingIntersection]  # within lookahead_m, nearest first
    lookahead_m: float
    remaining_signalised_on_route: int    # all signalised intersections still ahead, any distance
    crossed: List[IntersectionCrossing]
    signalised_crossed: int
    junctions_crossed: int                # every junction passed, signalised or not

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))