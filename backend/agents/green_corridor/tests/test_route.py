"""Route planning (testing requirements #3, #7, #8) and recalculation when conditions change."""
import json
import xml.etree.ElementTree as ET

import pytest

from backend.agents.green_corridor import config
from backend.agents.green_corridor.connection import SumoConnection
from backend.agents.green_corridor.emergency_detector import detect_emergency_vehicle, get_emergency_status
from backend.agents.green_corridor.route_planner import (
    RoutePlanner, calculate_emergency_route, check_emergency_route, recalculate_emergency_route,
    should_recalculate)
from .conftest import requires_sumo

ORIGIN, HOSPITAL = "A0A1", "E3E4"


def _net(scenario):
    import sumolib
    return sumolib.net.readNet(scenario.net_file)


def _close(sc, edge):
    sc.traci.lane.setDisallowed(f"{edge}_0", ["all"])


def _wait_for_ambulance(sc, det, extra_steps=0):
    while not detect_emergency_vehicle(det)["data"]["count"]:
        sc.step()
    for _ in range(extra_steps):
        sc.step()


def _drive_to_arrival(sc, det, limit=600):
    roads = []
    while sc.traci.simulation.getTime() < limit:
        sc.step()
        r = get_emergency_status(det, "amb_1")
        if r["success"]:
            roads.append(r["data"]["road_id"])
            if r["data"]["status"] == "ARRIVED":
                return r["data"], roads
    raise AssertionError("ambulance never arrived")


# ---- pure decision rule (no SUMO) -----------------------------------------------------------
@pytest.mark.parametrize("current,best,expected", [
    (100, 95, False),      # saves 5 s: not worth it
    (100, 70, True),       # saves 30 s (30%)
    (300, 275, False),     # 25 s but only 8%
    (300, 250, True),      # 50 s, 16%
    (100, 100, False), (80, 120, False),
])
def test_recalculation_rule(current, best, expected):
    assert should_recalculate(current, best) is expected


@requires_sumo
def test_live_times_are_bounded_and_start_at_free_flow(sim):
    sc, _ = sim
    p = RoutePlanner(sc)
    for _ in range(3):
        sc.step()
    for e, t in p.live_times().items():
        ff = p.free_flow_s(e)
        assert ff - 1e-9 <= t <= ff * config.MAX_EDGE_DELAY_FACTOR + 1e-9


# ---- route calculation -----------------------------------------------------------------------
@requires_sumo
def test_route_is_a_valid_connected_path_on_the_real_network(sim, scenario):
    sc, _ = sim
    r = calculate_emergency_route(RoutePlanner(sc), HOSPITAL, origin_edge=ORIGIN)
    assert r["success"], r["error"]
    d = r["data"]
    assert d["edges"][0] == ORIGIN and d["edges"][-1] == HOSPITAL and d["num_edges"] == len(d["edges"])
    net = _net(scenario)
    for a, b in zip(d["edges"], d["edges"][1:]):                 # each hop is a real connection
        assert net.getEdge(b) in net.getEdge(a).getOutgoing()
    assert d["vehicle_type"] == "ambulance" and d["routing_mode"] == "live_traffic_dijkstra"


@requires_sumo
def test_distance_time_and_intersections(sim, scenario):
    sc, _ = sim
    d = calculate_emergency_route(RoutePlanner(sc), HOSPITAL, origin_edge=ORIGIN)["data"]
    net = _net(scenario)
    assert 2000 < d["distance_m"] < 2600
    assert d["free_flow_travel_time_s"] > 100
    assert d["estimated_travel_time_s"] >= d["free_flow_travel_time_s"] * 0.95
    assert d["congestion_factor"] >= 0.95
    # intersections == signalised junctions between consecutive route edges, in order
    tls = {t.getID() for t in net.getTrafficLights()}
    expected = [e for e in d["edges"][:-1] if net.getEdge(e).getToNode().getID() in tls]
    assert [i["approach_edge"] for i in d["intersections"]] == expected
    assert d["num_intersections"] == len(expected) >= 5
    for i in d["intersections"]:
        assert i["tls_id"] == net.getEdge(i["approach_edge"]).getToNode().getID()
        assert i["exit_edge"] == d["edges"][i["route_index"] + 1]
    dist = [i["distance_from_start_m"] for i in d["intersections"]]
    eta = [i["eta_from_start_s"] for i in d["intersections"]]
    assert dist == sorted(dist) and eta == sorted(eta) and len(set(dist)) == len(dist)
    assert 0 < dist[0] and dist[-1] < d["distance_m"] and eta[-1] < d["estimated_travel_time_s"]


@requires_sumo
def test_plan_agrees_with_sumos_own_router(sim):
    """Cross-check against SUMO's findRoute. The grid has equally short alternatives, so the edge lists may
    differ; what must agree is the distance (same network model) and that ours is no slower under live times."""
    sc, det = sim
    _wait_for_ambulance(sc, det)
    tc, planner = sc.traci, RoutePlanner(sc)
    d = calculate_emergency_route(planner, HOSPITAL, origin_edge=ORIGIN)["data"]
    stage = tc.simulation.findRoute(ORIGIN, HOSPITAL, "ambulance")
    assert d["distance_m"] == pytest.approx(stage.length, rel=0.01)
    times = planner.live_times()
    assert planner.estimate_time_s(d["edges"], times) <= planner.estimate_time_s(list(stage.edges), times) + 1e-6
    # route length formula matches SUMO for SUMO's own route too (validates the junction-length model)
    assert planner.route_length_m(list(stage.edges)) == pytest.approx(stage.length, rel=0.01)


@requires_sumo
def test_no_u_turns_in_planned_routes(sim, scenario):
    sc, _ = sim
    p = RoutePlanner(sc)
    d = calculate_emergency_route(p, "A0B0", origin_edge="E3E4")["data"]     # forces a long way round
    for a, b in zip(d["edges"], d["edges"][1:]):
        assert p.graph()[a][b]["direction"] not in ("t", "T")
        assert b != _reverse(a)


def _reverse(edge):            # grid edge ids are 'XYXY'-style: reverse of A0A1 is A1A0
    return edge[2:] + edge[:2]


@requires_sumo
def test_estimate_is_sane_against_measured_journey(scenario):
    """The estimate excludes signal waits, so it must sit below the measured time but not wildly so."""
    with SumoConnection() as sc:
        sc.start(scenario.baseline_cfg)
        from backend.agents.green_corridor.emergency_detector import EmergencyDetector
        det = EmergencyDetector(sc)
        _wait_for_ambulance(sc, det)
        planner = RoutePlanner(sc)
        d = calculate_emergency_route(planner, HOSPITAL, vehicle_id="amb_1")["data"]
        _drive_to_arrival(sc, det)
    amb = next(x for x in ET.parse(scenario.baseline_tripinfo).getroot().iter("tripinfo")
               if x.get("id") == "amb_1")
    actual = float(amb.get("duration"))
    assert actual >= d["free_flow_travel_time_s"] * 0.95          # cannot beat the speed limit
    assert 0.5 <= d["estimated_travel_time_s"] / actual <= 1.15, (d["estimated_travel_time_s"], actual)


# ---- invalid input / unavailable routes -------------------------------------------------------
@requires_sumo
@pytest.mark.parametrize("origin,dest,code", [
    (ORIGIN, "NOPE", "UNKNOWN_EDGE"), ("NOPE", HOSPITAL, "UNKNOWN_EDGE"),
    (ORIGIN, ":B1_0", "UNKNOWN_EDGE"),                            # junction-internal ids are not destinations
    (ORIGIN, ORIGIN, "ORIGIN_EQUALS_DESTINATION"),
    (ORIGIN, "", "INVALID_INPUT"), (ORIGIN, None, "INVALID_INPUT"), (ORIGIN, 42, "INVALID_INPUT"),
    ("", HOSPITAL, "INVALID_INPUT"),
])
def test_invalid_destinations_and_origins(sim, origin, dest, code):
    sc, _ = sim
    r = calculate_emergency_route(RoutePlanner(sc), dest, origin_edge=origin)
    assert r["success"] is False and r["error"]["code"] == code
    json.dumps(r)


@requires_sumo
def test_missing_origin_and_unknown_vehicle(sim):
    sc, _ = sim
    p = RoutePlanner(sc)
    assert calculate_emergency_route(p, HOSPITAL)["error"]["code"] == "INVALID_INPUT"
    assert calculate_emergency_route(p, HOSPITAL, vehicle_id="ghost")["error"]["code"] == "VEHICLE_NOT_FOUND"
    assert check_emergency_route(p, "ghost")["error"]["code"] == "VEHICLE_NOT_FOUND"


@requires_sumo
def test_closed_destination_and_origin_are_reported(sim):
    sc, _ = sim
    p = RoutePlanner(sc)
    _close(sc, HOSPITAL)
    assert calculate_emergency_route(p, HOSPITAL, origin_edge=ORIGIN)["error"]["code"] == "DESTINATION_CLOSED"
    _close(sc, ORIGIN)
    assert calculate_emergency_route(p, "D3E3", origin_edge=ORIGIN)["error"]["code"] == "ORIGIN_CLOSED"


@requires_sumo
def test_unreachable_destination_cut_off_by_closures(sim, scenario):
    """Destination itself is open, but every road leading into it is closed -> no route, clean error."""
    sc, _ = sim
    net = _net(scenario)
    feeders = [e.getID() for e in net.getEdge(HOSPITAL).getIncoming()]
    assert feeders, "test premise: destination has feeder roads"
    for e in feeders:
        _close(sc, e)
    r = calculate_emergency_route(RoutePlanner(sc), HOSPITAL, origin_edge=ORIGIN)
    assert r["success"] is False and r["error"]["code"] == "NO_ROUTE_AVAILABLE"


@requires_sumo
def test_closed_road_on_route_is_avoided_by_a_new_plan(sim):
    sc, _ = sim
    p = RoutePlanner(sc)
    base = calculate_emergency_route(p, HOSPITAL, origin_edge=ORIGIN)["data"]
    victim = base["edges"][3]
    _close(sc, victim)
    detour = calculate_emergency_route(p, HOSPITAL, origin_edge=ORIGIN)["data"]
    assert victim not in detour["edges"] and detour["edges"][-1] == HOSPITAL
    assert detour["estimated_travel_time_s"] >= base["estimated_travel_time_s"] - 1e-6   # base was optimal


# ---- recalculation when conditions change -----------------------------------------------------
@requires_sumo
def test_no_recalculation_needed_in_normal_conditions(sim):
    sc, det = sim
    _wait_for_ambulance(sc, det, extra_steps=5)
    c = check_emergency_route(RoutePlanner(sc), "amb_1")
    assert c["success"] and c["data"]["needs_recalculation"] is False
    assert c["data"]["reason"] is None and c["data"]["blocked_edges"] == []


@requires_sumo
def test_blocked_road_ahead_triggers_reroute_and_ambulance_still_arrives(sim):
    sc, det = sim
    _wait_for_ambulance(sc, det, extra_steps=10)
    p = RoutePlanner(sc)
    victim = "A3B3"
    assert victim in sc.traci.vehicle.getRoute("amb_1")
    _close(sc, victim)
    c = check_emergency_route(p, "amb_1")["data"]
    assert c["needs_recalculation"] and c["reason"] == "ROUTE_BLOCKED" and c["blocked_edges"] == [victim]
    assert c["alternative_available"]
    r = recalculate_emergency_route(p, "amb_1")
    assert r["success"], r["error"]
    assert victim not in r["data"]["edges"] and r["data"]["reason"] == "ROUTE_BLOCKED"
    assert r["data"]["applied_to_vehicle"] == "amb_1"
    assert victim not in sc.traci.vehicle.getRoute("amb_1")               # vehicle really follows it
    final, roads = _drive_to_arrival(sc, det)
    assert final["status"] == "ARRIVED" and final["destination_edge"] == HOSPITAL
    assert victim not in roads
    assert check_emergency_route(p, "amb_1")["error"] is not None or True  # vehicle gone: must not crash


@requires_sumo
def test_route_cut_off_ahead_reports_no_alternative(sim, scenario):
    sc, det = sim
    _wait_for_ambulance(sc, det, extra_steps=5)
    for e in [x.getID() for x in _net(scenario).getEdge(HOSPITAL).getIncoming()]:
        _close(sc, e)
    p = RoutePlanner(sc)
    c = check_emergency_route(p, "amb_1")["data"]
    assert c["needs_recalculation"] and c["reason"] == "ROUTE_BLOCKED" and c["alternative_available"] is False
    r = recalculate_emergency_route(p, "amb_1")
    assert r["success"] is False and r["error"]["code"] == "NO_ROUTE_AVAILABLE"


@requires_sumo
def test_standing_traffic_jam_ahead_suggests_faster_alternative(sim):
    sc, det = sim
    tc = sc.traci
    _wait_for_ambulance(sc, det, extra_steps=5)
    jam_edge = tc.vehicle.getRoute("amb_1")[4]                    # a road well ahead on ITS route
    tc.route.add("jam_route", [jam_edge])
    tc.vehicle.add("jam", "jam_route", typeID="car", departPos="100")
    sc.step()
    assert "jam" in tc.vehicle.getIDList()
    tc.vehicle.setSpeedMode("jam", 0)
    tc.vehicle.setSpeed("jam", 0)
    for _ in range(6):                                            # let it brake to a standstill
        sc.step()
    assert tc.vehicle.getSpeed("jam") == 0
    p = RoutePlanner(sc)
    assert p.live_times()[jam_edge] == pytest.approx(p.free_flow_s(jam_edge) * config.MAX_EDGE_DELAY_FACTOR)
    c = check_emergency_route(p, "amb_1")["data"]
    assert c["needs_recalculation"] and c["reason"] == "FASTER_ALTERNATIVE", c
    assert c["blocked_edges"] == [] and c["estimated_gain_s"] >= config.RECALC_MIN_GAIN_S
    r = recalculate_emergency_route(p, "amb_1")["data"]
    assert jam_edge not in r["edges"]


@requires_sumo
def test_reroute_while_inside_a_junction(sim):
    """Applying a route while the vehicle is inside a junction must not break the vehicle."""
    sc, det = sim
    tc = sc.traci
    _wait_for_ambulance(sc, det)
    while not tc.vehicle.getRoadID("amb_1").startswith(":"):
        sc.step()
    p = RoutePlanner(sc)
    r = recalculate_emergency_route(p, "amb_1")
    assert r["success"], r["error"]
    assert tc.vehicle.getRoute("amb_1")[-1] == HOSPITAL
    final, _ = _drive_to_arrival(sc, det)
    assert final["status"] == "ARRIVED"
    assert r["data"]["applied_to_vehicle"] == "amb_1" and r["data"]["edges"][-1] == HOSPITAL


# ---- connection handling ------------------------------------------------------------------------
@requires_sumo
def test_connection_loss_and_not_connected(sim):
    sc, _ = sim
    p = RoutePlanner(sc)
    sc.traci._process.kill()
    sc.traci._process.wait()
    r = calculate_emergency_route(p, HOSPITAL, origin_edge=ORIGIN)
    assert r["success"] is False and r["error"]["code"] == "CONNECTION_LOST"
    r = calculate_emergency_route(RoutePlanner(SumoConnection()), HOSPITAL, origin_edge=ORIGIN)
    assert r["success"] is False and r["error"]["code"] == "NOT_CONNECTED"