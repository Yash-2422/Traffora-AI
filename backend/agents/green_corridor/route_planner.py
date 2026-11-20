"""Emergency route planning on the real SUMO network.

The road graph is built from the live simulation through TraCI (edges = nodes, lane connections = links)
and searched with NetworkX Dijkstra. Edge weights are CURRENT travel times derived from the speeds of the
vehicles on each edge (harmonic mean, capped, see config.MAX_EDGE_DELAY_FACTOR), so the planner reacts to queues; closed roads (vehicle class not allowed)
are excluded. This is read-only: it never changes edge weights or permissions in SUMO, so it cannot
disturb other traffic.

ETAs exclude waiting at signals: real journey time is measured from the simulation later (Step 8).

Public, orchestrator-facing functions (never raise; return the JSON envelope):
    calculate_emergency_route(planner, destination_edge, vehicle_id=None, origin_edge=None, apply=False)
    check_emergency_route(planner, vehicle_id)
    recalculate_emergency_route(planner, vehicle_id, apply=True)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx

from . import config
from .api_utils import guarded
from .schemas import EmergencyRoute, RouteCheck, RouteIntersection

logger = logging.getLogger(__name__)

ROUTING_METHOD = "live_traffic_dijkstra"
_LINK_VIA, _LINK_DIRECTION, _LINK_LENGTH = 4, 6, 7      # indices in TraCI lane.getLinks() tuples


class RouteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_traci(conn: Any):
    return conn.traci if hasattr(conn, "traci") else conn


def should_recalculate(current_eta_s: float, best_eta_s: float,
                       min_gain_s: float = config.RECALC_MIN_GAIN_S,
                       min_fraction: float = config.RECALC_MIN_GAIN_FRACTION) -> bool:
    """Pure decision rule: re-route only for a meaningful gain (avoids flip-flopping)."""
    gain = current_eta_s - best_eta_s
    return gain >= min_gain_s and gain >= min_fraction * current_eta_s


def signalised_approaches(tc) -> Dict[str, str]:
    """Map 'edge entering a signalised junction' -> traffic light id (pure TraCI, no net file needed)."""
    mapping: Dict[str, str] = {}
    for tls in tc.trafficlight.getIDList():
        for lane in set(tc.trafficlight.getControlledLanes(tls)):
            mapping[tc.lane.getEdgeID(lane)] = tls
    return mapping


class RoutePlanner:
    def __init__(self, connection: Any, vtype_id: str = config.AMBULANCE_VTYPE_ID,
                 vclass: str = config.EMERGENCY_VCLASS, allow_u_turns: bool = config.ALLOW_U_TURNS):
        self._conn = connection
        self.vtype_id = vtype_id
        self.vclass = vclass
        self.allow_u_turns = allow_u_turns
        self._graph: Optional[nx.DiGraph] = None
        self._signal_map: Optional[Dict[str, str]] = None
        self._active: Dict[str, EmergencyRoute] = {}

    @property
    def _tc(self):
        return _resolve_traci(self._conn)

    # ---- network graph (built once from the live simulation) ----------------------------
    def graph(self) -> nx.DiGraph:
        """Directed graph: node = edge id, link = lane connection (attrs: junction_m, direction)."""
        if self._graph is None:
            tc, g = self._tc, nx.DiGraph()
            edges = sorted(e for e in tc.edge.getIDList() if not e.startswith(":"))
            for e in edges:
                g.add_node(e, length_m=tc.lane.getLength(f"{e}_0"),
                           max_speed=tc.lane.getMaxSpeed(f"{e}_0"))
            for e in edges:
                for i in range(tc.edge.getLaneNumber(e)):
                    for link in tc.lane.getLinks(f"{e}_{i}"):
                        nxt = tc.lane.getEdgeID(link[0])
                        direction = link[_LINK_DIRECTION]
                        if nxt == e or nxt not in g or (direction in ("t", "T") and not self.allow_u_turns):
                            continue
                        jlen = float(link[_LINK_LENGTH])
                        if g.has_edge(e, nxt):
                            jlen = min(jlen, g[e][nxt]["junction_m"])
                        g.add_edge(e, nxt, junction_m=jlen, direction=direction)
            self._graph = g
            logger.info("Road graph built: %d edges, %d connections", g.number_of_nodes(), g.number_of_edges())
        return self._graph

    def _signals(self) -> Dict[str, str]:
        if self._signal_map is None:
            self._signal_map = signalised_approaches(self._tc)
        return self._signal_map

    def _validate_edge(self, edge: Any, label: str) -> str:
        if not isinstance(edge, str) or not edge.strip():
            raise RouteError("INVALID_INPUT", f"{label} must be a non-empty edge id string")
        if edge not in self.graph():
            raise RouteError("UNKNOWN_EDGE", f"{label} '{edge}' is not a road in the simulation network")
        return edge

    def is_edge_open(self, edge: str, vclass: Optional[str] = None) -> bool:
        """True if at least one lane of the edge currently allows the vehicle class."""
        tc, vclass = self._tc, vclass or self.vclass
        return any(vclass in tc.lane.getAllowed(f"{edge}_{i}") for i in range(tc.edge.getLaneNumber(edge)))

    # ---- live travel times ---------------------------------------------------------------
    def free_flow_s(self, edge: str) -> float:
        n = self.graph().nodes[edge]
        return n["length_m"] / max(n["max_speed"], 0.1)

    def _edge_live_s(self, edge: str) -> float:
        """Live travel time for one edge from the harmonic mean speed of the vehicles on it.

        The harmonic mean is the correct average for travel time: one standing vehicle dominates, as it does
        for anyone queuing behind it (SUMO's own getTraveltime uses the arithmetic mean, which hides it).
        Result is bounded to [free-flow, MAX_EDGE_DELAY_FACTOR x free-flow].
        """
        tc, node = self._tc, self.graph().nodes[edge]
        ff = self.free_flow_s(edge)
        ids = tc.edge.getLastStepVehicleIDs(edge)
        if not ids:
            return ff
        inv = sum(1.0 / max(tc.vehicle.getSpeed(v), config.HARMONIC_MIN_SPEED_MPS) for v in ids)
        harmonic_speed = len(ids) / inv
        return min(max(node["length_m"] / harmonic_speed, ff), ff * config.MAX_EDGE_DELAY_FACTOR)

    def live_times(self) -> Dict[str, float]:
        """Current travel time of every edge (see _edge_live_s)."""
        return {e: self._edge_live_s(e) for e in self.graph().nodes}

    @staticmethod
    def estimate_time_s(edges: List[str], times: Dict[str, float], first_edge_fraction: float = 1.0) -> float:
        return sum(times[e] * (first_edge_fraction if i == 0 else 1.0) for i, e in enumerate(edges))

    def route_length_m(self, edges: List[str], first_edge_fraction: float = 1.0) -> float:
        """Lane lengths plus junction-crossing lengths (matches SUMO's own route length)."""
        g = self.graph()
        total = sum(g.nodes[e]["length_m"] * (first_edge_fraction if i == 0 else 1.0)
                    for i, e in enumerate(edges))
        return total + sum(g[a][b]["junction_m"] for a, b in zip(edges, edges[1:]))

    def _shortest(self, origin: str, dest: str, times: Dict[str, float]) -> List[str]:
        """Fastest open path origin -> dest under live times; [] if none."""
        g = self.graph()
        open_nodes = {e for e in g.nodes if self.is_edge_open(e)}
        view = nx.subgraph_view(g, filter_node=lambda n: n in open_nodes)
        try:
            return nx.dijkstra_path(view, origin, dest, weight=lambda u, v, d: times[v] + 1e-9)
        except nx.NetworkXNoPath:
            return []

    # ---- planning ---------------------------------------------------------------------------
    def _describe(self, origin: str, dest: str, edges: List[str], times: Dict[str, float], vtype: str,
                  fraction: float = 1.0, reason: Optional[str] = None) -> EmergencyRoute:
        tc, g, signals = self._tc, self.graph(), self._signals()
        inters: List[RouteIntersection] = []
        cum_d = cum_t = cum_f = 0.0
        for i, e in enumerate(edges):
            frac = fraction if i == 0 else 1.0
            cum_d += g.nodes[e]["length_m"] * frac
            cum_t += times[e] * frac
            cum_f += self.free_flow_s(e) * frac
            if i < len(edges) - 1:
                cum_d += g[e][edges[i + 1]]["junction_m"]
                if e in signals:                                  # junction between edge i and i+1
                    tls = signals[e]
                    try:
                        x, y = tc.junction.getPosition(tls)
                    except Exception:
                        x = y = None
                    inters.append(RouteIntersection(tls_id=tls, approach_edge=e, exit_edge=edges[i + 1],
                                                    route_index=i, distance_from_start_m=cum_d,
                                                    eta_from_start_s=cum_t, x=x, y=y))
        return EmergencyRoute(
            origin_edge=origin, destination_edge=dest, vehicle_type=vtype, edges=edges, num_edges=len(edges),
            distance_m=self.route_length_m(edges, fraction), estimated_travel_time_s=cum_t,
            free_flow_travel_time_s=cum_f, congestion_factor=(cum_t / cum_f if cum_f > 0 else 1.0),
            intersections=inters, num_intersections=len(inters), routing_mode=ROUTING_METHOD,
            computed_at_sim_time_s=tc.simulation.getTime(), reason=reason)

    def calculate_route(self, origin_edge: str, destination_edge: str, vehicle_type: Optional[str] = None,
                        first_edge_fraction: float = 1.0, reason: Optional[str] = None,
                        times: Optional[Dict[str, float]] = None) -> EmergencyRoute:
        origin = self._validate_edge(origin_edge, "origin_edge")
        dest = self._validate_edge(destination_edge, "destination_edge")
        if origin == dest:
            raise RouteError("ORIGIN_EQUALS_DESTINATION", "origin and destination are the same edge")
        if not self.is_edge_open(origin):
            raise RouteError("ORIGIN_CLOSED", f"origin edge '{origin}' is closed to emergency vehicles")
        if not self.is_edge_open(dest):
            raise RouteError("DESTINATION_CLOSED", f"destination edge '{dest}' is closed to emergency vehicles")
        times = times or self.live_times()
        edges = self._shortest(origin, dest, times)
        if not edges:
            raise RouteError("NO_ROUTE_AVAILABLE",
                             f"no route from '{origin}' to '{dest}' with the current road availability")
        return self._describe(origin, dest, edges, times, vehicle_type or self.vtype_id,
                              first_edge_fraction, reason)

    # ---- vehicle-aware planning -------------------------------------------------------------
    def _vehicle_context(self, vehicle_id: str):
        if not isinstance(vehicle_id, str) or not vehicle_id.strip():
            raise RouteError("INVALID_INPUT", "vehicle_id must be a non-empty string")
        tc = self._tc
        if vehicle_id not in tc.vehicle.getIDList():
            raise RouteError("VEHICLE_NOT_FOUND", f"'{vehicle_id}' is not in the simulation")
        route = list(tc.vehicle.getRoute(vehicle_id))
        idx = tc.vehicle.getRouteIndex(vehicle_id)
        road = tc.vehicle.getRoadID(vehicle_id)
        if road.startswith(":"):                 # inside a junction: plan from the committed next edge
            prefix = [route[idx]]
            origin = route[idx + 1] if idx + 1 < len(route) else None
            fraction = 1.0
        else:
            prefix, origin = [], road
            length = tc.lane.getLength(tc.vehicle.getLaneID(vehicle_id))
            fraction = max(0.0, 1.0 - tc.vehicle.getLanePosition(vehicle_id) / length) if length > 0 else 1.0
        return route, idx, prefix, origin, fraction

    def plan_for_vehicle(self, vehicle_id: str, destination_edge: Optional[str] = None,
                         apply: bool = False, reason: Optional[str] = None) -> EmergencyRoute:
        """Plan from the vehicle's current position. apply=True makes the vehicle follow the new route."""
        route, idx, prefix, origin, fraction = self._vehicle_context(vehicle_id)
        dest = destination_edge if destination_edge is not None else route[-1]
        self._validate_edge(dest, "destination_edge")
        if origin is None or origin == dest:
            raise RouteError("ALREADY_AT_DESTINATION", f"'{vehicle_id}' is already on its destination edge")
        planned = self.calculate_route(origin, dest, self._tc.vehicle.getTypeID(vehicle_id), fraction, reason)
        if apply:
            try:
                self._tc.vehicle.setRoute(vehicle_id, prefix + planned.edges)
            except Exception as exc:
                if type(exc).__name__ != "TraCIException":
                    raise
                raise RouteError("ROUTE_APPLY_FAILED", f"vehicle could not follow the new route: {exc}")
            planned.applied_to_vehicle = vehicle_id
            logger.info("Route applied to %s: %s", vehicle_id, " ".join(planned.edges))
        self._active[vehicle_id] = planned
        return planned

    def get_active_route(self, vehicle_id: str) -> Optional[EmergencyRoute]:
        return self._active.get(vehicle_id)

    def check_route(self, vehicle_id: str) -> RouteCheck:
        """Compare the vehicle's current route with present conditions and decide if re-planning is needed."""
        tc = self._tc
        route, idx, prefix, origin, fraction = self._vehicle_context(vehicle_id)
        dest = route[-1]
        if origin is None or origin == dest:
            raise RouteError("ALREADY_AT_DESTINATION", f"'{vehicle_id}' is already on its destination edge")
        remaining = route[idx + 1:] if prefix else route[idx:]
        ahead = remaining if prefix else remaining[1:]
        blocked = [e for e in ahead if not self.is_edge_open(e)]
        times = self.live_times()
        current_eta = self.estimate_time_s(remaining, times, fraction)

        best: List[str] = []
        if self.is_edge_open(origin) and self.is_edge_open(dest):
            best = self._shortest(origin, dest, times)
        best_eta = self.estimate_time_s(best, times, fraction) if best else None

        if blocked:
            reason = "ROUTE_BLOCKED"
        elif best and best != remaining and should_recalculate(current_eta, best_eta):
            reason = "FASTER_ALTERNATIVE"
        else:
            reason = None
        return RouteCheck(vehicle_id=vehicle_id, sim_time_s=tc.simulation.getTime(),
                          needs_recalculation=reason is not None, reason=reason, blocked_edges=blocked,
                          alternative_available=bool(best), current_remaining_eta_s=current_eta,
                          best_alternative_eta_s=best_eta,
                          estimated_gain_s=(current_eta - best_eta) if best_eta is not None else None)


# ---- public, orchestrator-facing functions ----------------------------------------------------
def calculate_emergency_route(planner: RoutePlanner, destination_edge: str, vehicle_id: Optional[str] = None,
                              origin_edge: Optional[str] = None, apply: bool = False) -> Dict[str, Any]:
    """Calculate an emergency route on the live SUMO network (traffic- and closure-aware).

    Args:
        planner: RoutePlanner bound to a live SUMO/TraCI connection.
        destination_edge: SUMO edge id of the hospital approach, e.g. "E3E4".
        vehicle_id: if given, plan from that vehicle's current position (its vehicle type is used).
        origin_edge: required when vehicle_id is not given.
        apply: with vehicle_id, make the vehicle follow the planned route.
    Returns (envelope): data = EmergencyRoute dict {edges, distance_m, estimated_travel_time_s,
        free_flow_travel_time_s, intersections[{tls_id, approach_edge, exit_edge, distance_from_start_m,
        eta_from_start_s, ...}], ...}. ETAs exclude signal waiting.
    Errors: INVALID_INPUT, UNKNOWN_EDGE, ORIGIN_EQUALS_DESTINATION, ORIGIN_CLOSED, DESTINATION_CLOSED,
        NO_ROUTE_AVAILABLE, VEHICLE_NOT_FOUND, ALREADY_AT_DESTINATION, ROUTE_APPLY_FAILED,
        CONNECTION_LOST, NOT_CONNECTED, TRACI_ERROR.
    """
    def run():
        if vehicle_id is not None:
            return planner.plan_for_vehicle(vehicle_id, destination_edge, apply=apply).to_dict()
        if origin_edge is None:
            raise RouteError("INVALID_INPUT", "provide origin_edge or vehicle_id")
        return planner.calculate_route(origin_edge, destination_edge).to_dict()
    return guarded(run)


def check_emergency_route(planner: RoutePlanner, vehicle_id: str) -> Dict[str, Any]:
    """Monitor a vehicle's route: data = RouteCheck dict (needs_recalculation, reason, blocked_edges,
    current_remaining_eta_s, best_alternative_eta_s, estimated_gain_s). reason is ROUTE_BLOCKED when a road
    ahead is closed to emergency vehicles, or FASTER_ALTERNATIVE when another route is meaningfully quicker."""
    return guarded(lambda: planner.check_route(vehicle_id).to_dict())


def recalculate_emergency_route(planner: RoutePlanner, vehicle_id: str, apply: bool = True) -> Dict[str, Any]:
    """Re-plan from the vehicle's current position to its destination (and, by default, apply it).
    Returns data = EmergencyRoute dict. Errors as calculate_emergency_route (NO_ROUTE_AVAILABLE if cut off)."""
    def run():
        check = planner.check_route(vehicle_id)
        return planner.plan_for_vehicle(vehicle_id, apply=apply, reason=check.reason or "MANUAL").to_dict()
    return guarded(run)