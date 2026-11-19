"""Emergency vehicle detection and tracking via TraCI.

Call ``EmergencyDetector.update()`` once per simulation step (the agent does this). It finds emergency
vehicles, samples their state, keeps a trail, and notices arrivals (SUMO only reports an arrival for
one step, so skipping steps can lose it).

Public, orchestrator-facing functions (never raise; return the standard JSON envelope):
    detect_emergency_vehicle(detector)
    get_emergency_status(detector, vehicle_id)
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import replace
from typing import Any, Deque, Dict, List, Optional, Sequence

from . import config
from .connection import SumoConnectionError
from .schemas import EmergencyStatus, EmergencyVehicleState, Movement, fail, ok

logger = logging.getLogger(__name__)


class DetectorError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_traci(conn: Any):
    """Accept a SumoConnection (has .traci) or a raw TraCI connection object."""
    return conn.traci if hasattr(conn, "traci") else conn


class EmergencyDetector:
    def __init__(
        self,
        connection: Any,
        vtype_ids: Sequence[str] = config.EMERGENCY_VTYPE_IDS,
        vclass: str = config.EMERGENCY_VCLASS,
        id_prefixes: Sequence[str] = config.EMERGENCY_ID_PREFIXES,
    ):
        self._conn = connection
        self.vtype_ids = tuple(vtype_ids)
        self.vclass = vclass
        self.id_prefixes = tuple(id_prefixes)
        self._first_seen: Dict[str, float] = {}
        self._arrived: set = set()
        self._non_emergency: set = set()
        self._states: Dict[str, EmergencyVehicleState] = {}   # latest state per known vehicle
        self._tracks: Dict[str, Deque[Dict[str, Any]]] = {}
        self._last_update: Optional[float] = None

    @property
    def _tc(self):
        return _resolve_traci(self._conn)

    # ---- classification ----------------------------------------------------
    def is_emergency(self, vehicle_id: str) -> bool:
        """True if the vehicle's type id, vehicle class, or id prefix marks it as emergency."""
        if vehicle_id.startswith(self.id_prefixes):
            return True
        tc = self._tc
        return (tc.vehicle.getTypeID(vehicle_id) in self.vtype_ids
                or tc.vehicle.getVehicleClass(vehicle_id) == self.vclass)

    # ---- per-step tick -----------------------------------------------------
    def update(self) -> Dict[str, List[str]]:
        """Process the current simulation step. Idempotent for the same sim time."""
        tc = self._tc
        now = tc.simulation.getTime()
        if self._last_update == now:
            return {"new": [], "arrived": []}
        self._last_update = now

        present = set(tc.vehicle.getIDList())
        self._non_emergency &= present                      # forget vehicles that left
        new: List[str] = []
        for vid in present:
            if vid in self._first_seen or vid in self._non_emergency:
                continue
            try:
                if self.is_emergency(vid):
                    self._first_seen[vid] = now
                    new.append(vid)
                    logger.info("Emergency vehicle detected: %s at t=%.1f", vid, now)
                else:
                    self._non_emergency.add(vid)
            except Exception as exc:  # vehicle vanished between calls
                if type(exc).__name__ != "TraCIException":
                    raise

        arrived = [v for v in tc.simulation.getArrivedIDList()
                   if v in self._first_seen and v not in self._arrived]
        self._arrived.update(arrived)
        for vid in arrived:
            logger.info("Emergency vehicle arrived: %s at t=%.1f", vid, now)

        for vid in sorted(v for v in self._first_seen if v in present and v not in self._arrived):
            try:
                self._states[vid] = self._sample(vid, now)
            except Exception as exc:
                if type(exc).__name__ != "TraCIException":
                    raise
                logger.warning("Could not sample %s: %s", vid, exc)
        for vid in arrived:
            if vid in self._states:
                self._states[vid] = replace(self._states[vid], status=EmergencyStatus.ARRIVED,
                                            movement=Movement.STOPPED, speed_mps=0.0, speed_kmh=0.0,
                                            sim_time_s=now)
        return {"new": new, "arrived": arrived}

    def _sample(self, vid: str, now: float) -> EmergencyVehicleState:
        tc = self._tc
        veh = tc.vehicle
        route = list(veh.getRoute(vid))
        dest = route[-1] if route else None
        speed = veh.getSpeed(vid)
        accel = veh.getAcceleration(vid)
        road = veh.getRoadID(vid)

        remaining: Optional[float] = None
        if dest:
            d = veh.getDrivingDistance(vid, dest, tc.lane.getLength(f"{dest}_0"))
            remaining = d if d > config.SUMO_INVALID_DOUBLE / 2 else None

        if speed < config.STOPPED_SPEED_MPS:
            movement = Movement.STOPPED
        elif accel > config.ACCEL_THRESHOLD_MPS2:
            movement = Movement.ACCELERATING
        elif accel < -config.ACCEL_THRESHOLD_MPS2:
            movement = Movement.DECELERATING
        else:
            movement = Movement.CRUISING

        x, y = veh.getPosition(vid)
        status = EmergencyStatus.DETECTED if now == self._first_seen[vid] else EmergencyStatus.EN_ROUTE
        state = EmergencyVehicleState(
            vehicle_id=vid, vehicle_type=veh.getTypeID(vid), vehicle_class=veh.getVehicleClass(vid),
            sim_time_s=now, status=status, movement=movement, x=x, y=y,
            heading_deg=veh.getAngle(vid), speed_mps=speed, speed_kmh=speed * 3.6,
            acceleration_mps2=accel, road_id=road, lane_id=veh.getLaneID(vid),
            lane_position_m=veh.getLanePosition(vid), on_junction=road.startswith(":"),
            route_edges=route, route_index=veh.getRouteIndex(vid), destination_edge=dest,
            distance_travelled_m=veh.getDistance(vid), remaining_distance_m=remaining,
            waiting_time_s=veh.getWaitingTime(vid),
        )
        track = self._tracks.setdefault(vid, deque(maxlen=config.TRACK_HISTORY_LEN))
        track.append({"t": now, "x": x, "y": y, "speed_mps": speed, "road_id": road})
        return state

    # ---- queries -----------------------------------------------------------
    def detect(self) -> List[EmergencyVehicleState]:
        """States of all emergency vehicles currently active (departed, not yet arrived)."""
        self.update()
        return [self._states[v] for v in sorted(self._first_seen)
                if v not in self._arrived and v in self._states]

    def get_state(self, vehicle_id: str) -> EmergencyVehicleState:
        if not isinstance(vehicle_id, str) or not vehicle_id.strip():
            raise DetectorError("INVALID_INPUT", "vehicle_id must be a non-empty string")
        self.update()
        if vehicle_id in self._arrived:
            return self._states[vehicle_id]
        if vehicle_id in self._states:
            return self._states[vehicle_id]
        if vehicle_id in self._first_seen:
            raise DetectorError("VEHICLE_REMOVED",
                                f"'{vehicle_id}' left the simulation without a recorded arrival")
        if vehicle_id in self._non_emergency:
            raise DetectorError("NOT_EMERGENCY_VEHICLE", f"'{vehicle_id}' is not an emergency vehicle")
        raise DetectorError("VEHICLE_NOT_FOUND",
                            f"'{vehicle_id}' is not in the simulation (unknown, not yet departed, or removed)")

    def get_track(self, vehicle_id: str) -> List[Dict[str, Any]]:
        """Trail of recent positions (for visualisation)."""
        return list(self._tracks.get(vehicle_id, []))

    def known_vehicle_ids(self) -> List[str]:
        return sorted(self._first_seen)


# ---- public, orchestrator-facing functions ------------------------------------
def _guarded(fn):
    """Run fn and wrap the result/any failure in the standard envelope. Never raises."""
    try:
        return ok(fn())
    except (DetectorError, SumoConnectionError) as exc:
        return fail(exc.code, exc.message)
    except Exception as exc:
        name = type(exc).__name__
        if name == "FatalTraCIError" or isinstance(exc, (OSError, EOFError)):  # incl. broken socket
            return fail("CONNECTION_LOST", f"SUMO connection lost: {exc}")
        if name == "TraCIException":
            return fail("TRACI_ERROR", str(exc))
        logger.exception("Unexpected error")
        return fail("UNEXPECTED_ERROR", f"{name}: {exc}")


def detect_emergency_vehicle(detector: EmergencyDetector) -> Dict[str, Any]:
    """Find all active emergency vehicles.

    Args:
        detector: an EmergencyDetector bound to a live SUMO/TraCI connection.
    Returns (envelope): data = {"count": int, "vehicle_ids": [str], "vehicles": [state dict, ...]}.
    Errors: CONNECTION_LOST, NOT_CONNECTED, TRACI_ERROR, UNEXPECTED_ERROR.
    """
    def run():
        states = detector.detect()
        return {"count": len(states), "vehicle_ids": [s.vehicle_id for s in states],
                "vehicles": [s.to_dict() for s in states]}
    return _guarded(run)


def get_emergency_status(detector: EmergencyDetector, vehicle_id: str) -> Dict[str, Any]:
    """Current state of one emergency vehicle (position, speed, road, destination, status, ...).

    Args:
        detector: an EmergencyDetector bound to a live SUMO/TraCI connection.
        vehicle_id: SUMO vehicle id, e.g. "amb_1".
    Returns (envelope): data = EmergencyVehicleState as dict; after arrival status is "ARRIVED".
    Errors: INVALID_INPUT, VEHICLE_NOT_FOUND, NOT_EMERGENCY_VEHICLE, VEHICLE_REMOVED,
            CONNECTION_LOST, NOT_CONNECTED, TRACI_ERROR, UNEXPECTED_ERROR.
    """
    return _guarded(lambda: detector.get_state(vehicle_id).to_dict())