"""Intersection monitoring for an emergency vehicle.

Per vehicle it answers: which signalised intersection is next, how far, when will it arrive, what is the
signal doing right now for the ambulance's own movement, which intersection is it inside, and which
intersections has it already crossed (with waiting time per intersection).

Call ``update(vehicle_id)`` every simulation step (identify_upcoming_intersections() does it for you).
Crossings are detected from edge transitions, not from "inside the junction" samples, because a junction
can be crossed within a single 1 s step.

Public, orchestrator-facing function (never raises; returns the JSON envelope):
    identify_upcoming_intersections(monitor, vehicle_id, lookahead_m=None)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .api_utils import guarded
from .route_planner import RoutePlanner, signalised_approaches
from .schemas import (IntersectionCrossing, IntersectionStatus, SignalSnapshot, UpcomingIntersection)

logger = logging.getLogger(__name__)

_SIGNAL_NAMES = {"G": "GREEN", "g": "GREEN", "y": "YELLOW", "Y": "YELLOW", "r": "RED", "R": "RED",
                 "u": "RED_YELLOW", "o": "OFF", "O": "OFF"}


class IntersectionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def signal_name(state_char: Optional[str]) -> Optional[str]:
    """Human-readable meaning of one SUMO signal state character."""
    if state_char is None:
        return None
    return _SIGNAL_NAMES.get(state_char, "UNKNOWN")


def classify_phase_state(state: str) -> str:
    """CLEARANCE if any link shows yellow (a clearance interval is running), else GREEN if any link is
    green, else ALL_RED."""
    if any(c in "yY" for c in state):
        return "CLEARANCE"
    if any(c in "Gg" for c in state):
        return "GREEN"
    return "ALL_RED"


def junction_of_internal_edge(edge_id: str) -> str:
    """':A1_7' -> 'A1'."""
    return edge_id[1:].rsplit("_", 1)[0]


class _Track:
    def __init__(self):
        self.last_time: Optional[float] = None
        self.last_edge: Optional[str] = None
        self.inside_tls: Optional[str] = None
        self.entered_time: Optional[float] = None
        self.wait: Dict[Tuple[str, str], float] = {}
        self.crossings: List[IntersectionCrossing] = []
        self.junctions_crossed = 0
        self.active = True


class IntersectionMonitor:
    def __init__(self, connection: Any, planner: Optional[RoutePlanner] = None):
        self._conn = connection
        self._planner = planner or RoutePlanner(connection)
        self._signals_map: Optional[Dict[str, str]] = None
        self._logic_cache: Dict[str, Any] = {}
        self._tracks: Dict[str, _Track] = {}

    @property
    def _tc(self):
        return self._conn.traci if hasattr(self._conn, "traci") else self._conn

    def _signal_map(self) -> Dict[str, str]:
        if self._signals_map is None:
            self._signals_map = signalised_approaches(self._tc)
        return self._signals_map

    # ---- route geometry ------------------------------------------------------------------
    def _remaining_intersections(self, route: List[str], idx: int, on_junction: bool):
        """[(route_index, approach_edge, exit_edge, tls_id)] for signalised junctions still ahead."""
        sm, start = self._signal_map(), (idx + 1 if on_junction else idx)
        return [(i, route[i], route[i + 1], sm[route[i]])
                for i in range(start, len(route) - 1) if route[i] in sm]

    # ---- per-step tracking ------------------------------------------------------------------
    def update(self, vehicle_id: str) -> None:
        """Process the current step for one vehicle. Idempotent within a simulation step."""
        tc = self._tc
        now = tc.simulation.getTime()
        tr = self._tracks.setdefault(vehicle_id, _Track())
        if tr.last_time == now:
            return
        if vehicle_id not in tc.vehicle.getIDList():
            tr.active = False
            tr.last_time = now
            return
        tr.last_time = now
        veh, sm = tc.vehicle, self._signal_map()
        road, speed = veh.getRoadID(vehicle_id), veh.getSpeed(vehicle_id)
        route, idx = list(veh.getRoute(vehicle_id)), veh.getRouteIndex(vehicle_id)
        on_j = road.startswith(":")
        cur_edge = route[idx] if on_j else road        # last real edge the vehicle is on or leaving

        if tr.last_edge is not None and cur_edge != tr.last_edge:
            self._record_crossing(tr, tr.last_edge, cur_edge, now, speed)
        tr.last_edge = cur_edge

        if on_j:
            junction = junction_of_internal_edge(road)
            if sm.get(cur_edge) == junction and tr.inside_tls != junction:
                tr.inside_tls, tr.entered_time = junction, now

        if speed < config.STOPPED_SPEED_MPS:               # attribute standing time to the next signal
            nxt = veh.getNextTLS(vehicle_id)
            if nxt and nxt[0][2] <= config.WAIT_ATTRIBUTION_ZONE_M:
                for i, approach, _exit, tls in self._remaining_intersections(route, idx, on_j):
                    if tls == nxt[0][0]:
                        key = (tls, approach)
                        tr.wait[key] = tr.wait.get(key, 0.0) + tc.simulation.getDeltaT()
                        break

    def _record_crossing(self, tr: _Track, old_edge: str, new_edge: str, now: float, speed: float) -> None:
        tr.junctions_crossed += 1
        tls = self._signal_map().get(old_edge)
        if tls is None:
            return
        tr.crossings.append(IntersectionCrossing(
            tls_id=tls, approach_edge=old_edge, exit_edge=new_edge,
            entered_time_s=tr.entered_time if tr.inside_tls == tls else None,
            crossed_time_s=now, wait_time_s=tr.wait.pop((tls, old_edge), 0.0), speed_mps=speed))
        tr.inside_tls, tr.entered_time = None, None
        logger.info("Crossed intersection %s at t=%.0f", tls, now)

    def crossing_log(self, vehicle_id: str) -> List[IntersectionCrossing]:
        return list(self._tracks[vehicle_id].crossings) if vehicle_id in self._tracks else []

    # ---- signal inspection --------------------------------------------------------------------
    def _logic(self, tls_id: str):
        if tls_id not in self._logic_cache:
            logics = self._tc.trafficlight.getAllProgramLogics(tls_id)
            program = self._tc.trafficlight.getProgram(tls_id)
            self._logic_cache[tls_id] = next((l for l in logics if l.programID == program), logics[0])
        return self._logic_cache[tls_id]

    def green_phase_indices(self, tls_id: str, link_index: int) -> List[int]:
        """Phases of the signal program in which the given link shows green."""
        return [i for i, p in enumerate(self._logic(tls_id).phases)
                if 0 <= link_index < len(p.state) and p.state[link_index] in "Gg"]

    def signal_snapshot(self, tls_id: str, link_index: Optional[int] = None,
                        link_state: Optional[str] = None) -> SignalSnapshot:
        tc = self._tc
        state = tc.trafficlight.getRedYellowGreenState(tls_id)
        now = tc.simulation.getTime()
        nxt = tc.trafficlight.getNextSwitch(tls_id)
        return SignalSnapshot(
            tls_id=tls_id, program_id=tc.trafficlight.getProgram(tls_id),
            phase_index=tc.trafficlight.getPhase(tls_id), num_phases=len(self._logic(tls_id).phases),
            phase_state=state, phase_kind=classify_phase_state(state),
            phase_remaining_s=max(0.0, nxt - now), next_switch_time_s=nxt,
            ambulance_link_index=link_index, ambulance_signal=signal_name(link_state),
            green_phase_indices=self.green_phase_indices(tls_id, link_index) if link_index is not None else [])

    # ---- main query ------------------------------------------------------------------------------
    def status(self, vehicle_id: str, lookahead_m: Optional[float] = None) -> IntersectionStatus:
        if not isinstance(vehicle_id, str) or not vehicle_id.strip():
            raise IntersectionError("INVALID_INPUT", "vehicle_id must be a non-empty string")
        if lookahead_m is not None and (isinstance(lookahead_m, bool) or not isinstance(lookahead_m, (int, float))
                                        or lookahead_m <= 0):
            raise IntersectionError("INVALID_INPUT", "lookahead_m must be a positive number")
        lookahead = float(lookahead_m) if lookahead_m is not None else config.INTERSECTION_LOOKAHEAD_M

        tc = self._tc
        self.update(vehicle_id)
        tr = self._tracks[vehicle_id]
        now = tc.simulation.getTime()
        if vehicle_id not in tc.vehicle.getIDList():
            if tr.last_edge is None:
                del self._tracks[vehicle_id]
                raise IntersectionError("VEHICLE_NOT_FOUND", f"'{vehicle_id}' is not in the simulation")
            return IntersectionStatus(
                vehicle_id=vehicle_id, sim_time_s=now, vehicle_active=False, on_junction=False,
                current_intersection=None, next_intersection=None, upcoming=[], lookahead_m=lookahead,
                remaining_signalised_on_route=0, crossed=list(tr.crossings),
                signalised_crossed=len(tr.crossings), junctions_crossed=tr.junctions_crossed)

        veh, g = tc.vehicle, self._planner.graph()
        route, idx = list(veh.getRoute(vehicle_id)), veh.getRouteIndex(vehicle_id)
        road, speed = veh.getRoadID(vehicle_id), veh.getSpeed(vehicle_id)
        on_j = road.startswith(":")
        sumo_ahead = {tls: (link, dist, state) for tls, link, dist, state in veh.getNextTLS(vehicle_id)}
        times = self._planner.live_times()

        # cumulative ETA to each edge's stop line along the remaining route
        start = idx + 1 if on_j else idx
        if on_j or start >= len(route):
            frac = 1.0
        else:
            length = tc.lane.getLength(veh.getLaneID(vehicle_id))
            frac = max(0.0, 1.0 - veh.getLanePosition(vehicle_id) / length) if length > 0 else 1.0
        eta_at: Dict[int, float] = {}
        t = 0.0
        for i in range(start, len(route)):
            t += times[route[i]] * (frac if i == start else 1.0)
            eta_at[i] = t
            if i + 1 < len(route):
                t += g[route[i]][route[i + 1]]["junction_m"] / max(g.nodes[route[i]]["max_speed"], 0.1)

        ahead = self._remaining_intersections(route, idx, on_j)
        upcoming: List[UpcomingIntersection] = []
        for i, approach, exit_edge, tls in ahead:
            link, dist, state = sumo_ahead.get(tls, (None, None, None))
            if dist is None:
                continue                                     # SUMO does not list it: cannot place it
            if dist > lookahead:
                continue
            try:
                x, y = tc.junction.getPosition(tls)
            except Exception:
                x = y = None
            upcoming.append(UpcomingIntersection(
                order=len(upcoming) + 1, tls_id=tls, approach_edge=approach, exit_edge=exit_edge,
                route_index=i, distance_m=dist, eta_s=eta_at[i],
                eta_at_current_speed_s=(dist / speed) if speed > 1.0 else None, x=x, y=y,
                signal=self.signal_snapshot(tls, link, state)))

        nearest = None
        if ahead and ahead[0][3] in sumo_ahead:
            i, approach, exit_edge, tls = ahead[0]
            if upcoming and upcoming[0].tls_id == tls:
                nearest = upcoming[0]
            else:                                            # nearest signal is beyond the look-ahead
                link, dist, state = sumo_ahead[tls]
                nearest = UpcomingIntersection(
                    order=1, tls_id=tls, approach_edge=approach, exit_edge=exit_edge, route_index=i,
                    distance_m=dist, eta_s=eta_at[i], eta_at_current_speed_s=(dist / speed) if speed > 1.0 else None,
                    x=None, y=None, signal=self.signal_snapshot(tls, link, state))
        current = tr.inside_tls if on_j and tr.inside_tls else None
        return IntersectionStatus(
            vehicle_id=vehicle_id, sim_time_s=now, vehicle_active=True, on_junction=on_j,
            current_intersection=current, next_intersection=nearest, upcoming=upcoming, lookahead_m=lookahead,
            remaining_signalised_on_route=len(ahead), crossed=list(tr.crossings),
            signalised_crossed=len(tr.crossings), junctions_crossed=tr.junctions_crossed)


# ---- public, orchestrator-facing function ----------------------------------------------------
def identify_upcoming_intersections(monitor: IntersectionMonitor, vehicle_id: str,
                                    lookahead_m: Optional[float] = None) -> Dict[str, Any]:
    """Signalised intersections ahead of a vehicle, the one it is inside, and the ones it has crossed.

    Args:
        monitor: IntersectionMonitor bound to a live SUMO/TraCI connection.
        vehicle_id: SUMO vehicle id, e.g. "amb_1".
        lookahead_m: only list intersections whose stop line is within this distance (default
            config.INTERSECTION_LOOKAHEAD_M). next_intersection is always filled, even if farther.
    Returns (envelope): data = IntersectionStatus dict:
        upcoming[]: {order, tls_id, approach_edge, exit_edge, distance_m (to stop line), eta_s (excludes signal
        waiting), eta_at_current_speed_s, signal: {phase_index, phase_state, phase_kind, phase_remaining_s,
        ambulance_signal, ambulance_link_index, green_phase_indices, ...}};
        current_intersection; crossed[] with crossed_time_s and wait_time_s; signalised_crossed;
        junctions_crossed. After the vehicle leaves the simulation, vehicle_active is false and crossed[] is kept.
    Errors: INVALID_INPUT, VEHICLE_NOT_FOUND, CONNECTION_LOST, NOT_CONNECTED, TRACI_ERROR.
    Call every simulation step (or call monitor.update(vehicle_id)) so crossings are not missed.
    """
    return guarded(lambda: monitor.status(vehicle_id, lookahead_m).to_dict())