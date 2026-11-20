"""Intersection identification, signal phase identification and crossing tracking (requirements #4, #5)."""
import json
import xml.etree.ElementTree as ET

import pytest

from backend.agents.green_corridor import config
from backend.agents.green_corridor.connection import SumoConnection
from backend.agents.green_corridor.emergency_detector import EmergencyDetector, detect_emergency_vehicle
from backend.agents.green_corridor.intersection_monitor import (
    IntersectionMonitor, classify_phase_state, identify_upcoming_intersections, junction_of_internal_edge,
    signal_name)
from backend.agents.green_corridor.route_planner import RoutePlanner, recalculate_emergency_route
from .conftest import requires_sumo

HOSPITAL = "E3E4"


# ---- pure helpers (no SUMO) ------------------------------------------------------------------
@pytest.mark.parametrize("state,kind", [
    ("GggrrrGGg", "GREEN"), ("yyyrrrGyy", "CLEARANCE"), ("rrrGGgGrr", "GREEN"),
    ("rrryyyGrr", "CLEARANCE"), ("rrrrrrrrr", "ALL_RED"), ("", "ALL_RED")])
def test_classify_phase_state(state, kind):
    assert classify_phase_state(state) == kind


@pytest.mark.parametrize("ch,name", [("G", "GREEN"), ("g", "GREEN"), ("y", "YELLOW"), ("r", "RED"),
                                    ("u", "RED_YELLOW"), ("o", "OFF"), ("?", "UNKNOWN"), (None, None)])
def test_signal_name(ch, name):
    assert signal_name(ch) == name


def test_junction_of_internal_edge():
    assert junction_of_internal_edge(":A1_7") == "A1"
    assert junction_of_internal_edge(":cluster_12_34_5") == "cluster_12_34"


# ---- helpers ------------------------------------------------------------------------------------
def _start(sc, det, extra_steps=0):
    while not detect_emergency_vehicle(det)["data"]["count"]:
        sc.step()
    for _ in range(extra_steps):
        sc.step()


def _drive(sc, det, mon, on_step=None, limit=600):
    """Step until the ambulance is gone; calls identify every step. Returns the list of status dicts."""
    out = []
    while sc.traci.simulation.getTime() < limit:
        sc.step()
        detect_emergency_vehicle(det)
        r = identify_upcoming_intersections(mon, "amb_1")
        if r["success"]:
            out.append(r["data"])
            if on_step:
                on_step(sc, r["data"])
            if not r["data"]["vehicle_active"]:
                return out
    raise AssertionError("ambulance never arrived")


# ---- upcoming intersections ---------------------------------------------------------------------
@requires_sumo
def test_unknown_vehicle_and_invalid_inputs(sim):
    sc, det = sim
    mon = IntersectionMonitor(sc)
    assert identify_upcoming_intersections(mon, "amb_1")["error"]["code"] == "VEHICLE_NOT_FOUND"
    _start(sc, det)
    for bad in ("", "  ", None, 5):
        assert identify_upcoming_intersections(mon, bad)["error"]["code"] == "INVALID_INPUT"
    for bad in (0, -3, "far", True, [1]):
        assert identify_upcoming_intersections(mon, "amb_1", bad)["error"]["code"] == "INVALID_INPUT"


@requires_sumo
def test_upcoming_intersections_distance_order_and_lookahead(sim):
    sc, det = sim
    _start(sc, det, extra_steps=10)
    mon, tc = IntersectionMonitor(sc), sc.traci
    r = identify_upcoming_intersections(mon, "amb_1")
    assert r["success"], r["error"]
    d = r["data"]
    ups = d["upcoming"]
    assert d["vehicle_active"] and not d["on_junction"] and d["current_intersection"] is None
    assert [u["tls_id"] for u in ups] == ["A1", "A2"]                      # A3 is ~748 m away
    assert [u["order"] for u in ups] == [1, 2]
    assert d["next_intersection"]["tls_id"] == "A1" and d["remaining_signalised_on_route"] == 7
    dist = [u["distance_m"] for u in ups]
    assert dist == sorted(dist) and 0 < dist[0] < dist[1] <= config.INTERSECTION_LOOKAHEAD_M
    # independent geometry from lane lengths and junction link lengths
    g = mon._planner.graph()
    route = list(tc.vehicle.getRoute("amb_1"))
    idx = tc.vehicle.getRouteIndex("amb_1")
    d0 = g.nodes[route[idx]]["length_m"] - tc.vehicle.getLanePosition("amb_1")
    assert dist[0] == pytest.approx(d0, abs=1.0)
    d1 = d0 + g[route[idx]][route[idx + 1]]["junction_m"] + g.nodes[route[idx + 1]]["length_m"]
    assert dist[1] == pytest.approx(d1, abs=1.0)
    # eta is sane: not faster than the speed limit allows, ordered
    for u in ups:
        assert u["eta_s"] >= u["distance_m"] / 13.89 - 1.0
    assert ups[0]["eta_s"] < ups[1]["eta_s"]
    # wider look-ahead returns more, narrower returns fewer, next_intersection is always filled
    assert len(identify_upcoming_intersections(mon, "amb_1", 5000)["data"]["upcoming"]) == 7
    narrow = identify_upcoming_intersections(mon, "amb_1", 20)["data"]
    assert narrow["upcoming"] == [] and narrow["next_intersection"]["tls_id"] == "A1"
    json.dumps(r)


@requires_sumo
def test_signal_snapshot_matches_sumo_and_program(sim):
    sc, det = sim
    _start(sc, det, extra_steps=10)
    mon, tc = IntersectionMonitor(sc), sc.traci
    u = identify_upcoming_intersections(mon, "amb_1")["data"]["upcoming"][0]
    s, tls = u["signal"], u["tls_id"]
    assert s["tls_id"] == tls
    assert s["phase_index"] == tc.trafficlight.getPhase(tls)
    assert s["phase_state"] == tc.trafficlight.getRedYellowGreenState(tls)
    assert s["program_id"] == tc.trafficlight.getProgram(tls) and s["num_phases"] == 4
    assert s["phase_remaining_s"] == pytest.approx(tc.trafficlight.getNextSwitch(tls) - tc.simulation.getTime(), abs=0.01)
    assert s["phase_remaining_s"] >= 0
    # the ambulance's own link is read correctly from the state string
    li = s["ambulance_link_index"]
    assert li is not None and 0 <= li < len(s["phase_state"])
    char = s["phase_state"][li]
    assert s["ambulance_signal"] == signal_name(char)
    # phases in which that link is green really show green for that link in the program
    logic = tc.trafficlight.getAllProgramLogics(tls)[0]
    assert s["green_phase_indices"] and all(logic.phases[i].state[li] in "Gg" for i in s["green_phase_indices"])
    assert all(logic.phases[i].state[li] not in "Gg" for i in range(4) if i not in s["green_phase_indices"])


@requires_sumo
def test_signal_phase_changes_are_observed_over_time(sim):
    """Phase index/state must follow the program as simulation time advances (27 s green, 3 s clearance, ...)."""
    sc, det = sim
    _start(sc, det)
    mon = IntersectionMonitor(sc)
    seen_kinds, seen_signals = set(), set()
    for _ in range(70):
        sc.step()
        r = identify_upcoming_intersections(mon, "amb_1", 5000)
        if r["data"]["next_intersection"]:
            sig = r["data"]["next_intersection"]["signal"]
            seen_kinds.add(sig["phase_kind"])
            seen_signals.add(sig["ambulance_signal"])
        if not r["data"]["vehicle_active"]:
            break
    assert {"GREEN", "CLEARANCE"} <= seen_kinds


# ---- crossing tracking over the whole journey ------------------------------------------------------
@requires_sumo
def test_full_journey_crossings(sim, scenario):
    sc, det = sim
    mon = IntersectionMonitor(sc)
    inside_seen, remaining_counts = set(), []

    def watch(_sc, d):
        if d["current_intersection"]:
            inside_seen.add(d["current_intersection"])
        remaining_counts.append(d["remaining_signalised_on_route"])

    statuses = _drive(sc, det, mon, watch)
    final = statuses[-1]
    expected = ["A1", "A2", "A3", "B3", "C3", "D3", "E3"]                 # signals on A0A1..E3E4
    assert [c["tls_id"] for c in final["crossed"]] == expected
    assert final["signalised_crossed"] == 7 and final["junctions_crossed"] == 7
    assert final["vehicle_active"] is False and final["upcoming"] == []
    times = [c["crossed_time_s"] for c in final["crossed"]]
    assert times == sorted(times) and len(set(times)) == 7
    for c, nxt in zip(final["crossed"], final["crossed"][1:]):
        assert c["exit_edge"] == nxt["approach_edge"]                      # consecutive, gap-free chain
    assert all(c["entered_time_s"] is None or c["entered_time_s"] <= c["crossed_time_s"] for c in final["crossed"])
    assert remaining_counts == sorted(remaining_counts, reverse=True) and remaining_counts[0] == 7
    assert inside_seen <= set(expected) and len(inside_seen) >= 1           # 1 s steps may skip a junction
    # crossing log stays available after the vehicle is gone
    assert [c.tls_id for c in mon.crossing_log("amb_1")] == expected
    # waiting attributed to intersections never exceeds what SUMO measured for the whole trip
    waits = sum(c["wait_time_s"] for c in final["crossed"])
    sc.close()
    amb = next(x for x in ET.parse(scenario.baseline_tripinfo).getroot().iter("tripinfo") if x.get("id") == "amb_1")
    assert waits <= float(amb.get("waitingTime")) + 2.0
    assert waits >= 0


@requires_sumo
def test_waiting_time_is_attributed_to_the_right_intersection(sim):
    """Every crossing's wait_time_s must equal an independent count of stopped steps near that signal."""
    from collections import defaultdict
    sc, det = sim
    tc, mon = sc.traci, IntersectionMonitor(sc)
    counted = defaultdict(int)

    def count(d):
        n = d["next_intersection"]
        if (n and n["distance_m"] <= config.WAIT_ATTRIBUTION_ZONE_M
                and tc.vehicle.getSpeed("amb_1") < config.STOPPED_SPEED_MPS):
            counted[n["tls_id"]] += 1

    _start(sc, det, extra_steps=3)
    while True:                                                           # approach A2, then force a stop 90 m before it
        sc.step()
        detect_emergency_vehicle(det)
        d = identify_upcoming_intersections(mon, "amb_1")["data"]
        count(d)
        if d["next_intersection"]["tls_id"] == "A2" and d["next_intersection"]["distance_m"] < 90:
            break
    before_forced = counted["A2"]
    tc.vehicle.setSpeed("amb_1", 0)
    for _ in range(12):
        sc.step()
        count(identify_upcoming_intersections(mon, "amb_1")["data"])
    forced = counted["A2"] - before_forced
    tc.vehicle.setSpeed("amb_1", -1)                                       # hand control back to SUMO
    final = _drive(sc, det, mon, lambda _sc, d: count(d))[-1]

    assert forced >= 5
    for c in final["crossed"]:
        assert c["wait_time_s"] == pytest.approx(counted[c["tls_id"]], abs=1.0), c["tls_id"]
    a2 = next(c for c in final["crossed"] if c["tls_id"] == "A2")
    assert a2["wait_time_s"] >= forced


@requires_sumo
def test_eta_prediction_accuracy_when_not_stopping(scenario):
    """ETA given ~10 s before a crossing must match the real crossing time when the vehicle did not stop."""
    with SumoConnection() as sc:
        sc.start(scenario.baseline_cfg)
        det, mon = EmergencyDetector(sc), IntersectionMonitor(sc)
        _start(sc, det)
        samples, speeds = [], {}                                          # (t, tls, eta), t -> speed
        def collect(_sc, d):
            speeds[d["sim_time_s"]] = _sc.traci.vehicle.getSpeed("amb_1") if d["vehicle_active"] else 0
            if d["next_intersection"]:
                samples.append((d["sim_time_s"], d["next_intersection"]["tls_id"], d["next_intersection"]["eta_s"]))
        final = _drive(sc, det, mon, collect)[-1]
    checked = 0
    for c in final["crossed"]:
        window = [(t, eta) for t, tls, eta in samples if tls == c["tls_id"] and 5 <= c["crossed_time_s"] - t <= 12]
        if not window or any(speeds[t2] < 1.0 for t2 in speeds if window[0][0] <= t2 <= c["crossed_time_s"]):
            continue                                                       # it stopped for a red: ETA excludes that
        t0, eta = window[0]
        assert abs((t0 + eta) - c["crossed_time_s"]) <= 4.0, (c["tls_id"], t0, eta, c["crossed_time_s"])
        checked += 1
    assert checked >= 2, "expected at least two non-stopping crossings to validate the ETA"


@requires_sumo
def test_crossings_after_a_reroute_reflect_the_roads_actually_driven(sim):
    sc, det = sim
    tc = sc.traci
    mon, planner = IntersectionMonitor(sc), RoutePlanner(sc)
    _start(sc, det, extra_steps=10)
    tc.lane.setDisallowed("A3B3_0", ["all"])
    assert recalculate_emergency_route(planner, "amb_1")["success"]
    final = _drive(sc, det, mon)[-1]
    roads = []
    for p in det.get_track("amb_1"):
        if not p["road_id"].startswith(":") and (not roads or roads[-1] != p["road_id"]):
            roads.append(p["road_id"])
    sm = mon._signal_map()
    expected = [sm[a] for a in roads[:-1] if a in sm]
    assert "A3B3" not in roads
    assert [c["tls_id"] for c in final["crossed"]] == expected
    assert final["junctions_crossed"] == len(roads) - 1


@requires_sumo
def test_connection_loss_and_not_connected(sim):
    sc, det = sim
    mon = IntersectionMonitor(sc)
    _start(sc, det)
    sc.traci._process.kill()
    sc.traci._process.wait()
    r = identify_upcoming_intersections(mon, "amb_1")
    assert r["success"] is False and r["error"]["code"] == "CONNECTION_LOST"
    r = identify_upcoming_intersections(IntersectionMonitor(SumoConnection()), "amb_1")
    assert r["success"] is False and r["error"]["code"] == "NOT_CONNECTED"