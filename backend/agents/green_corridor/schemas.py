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