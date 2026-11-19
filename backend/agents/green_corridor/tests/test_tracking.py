"""Ambulance tracking over a whole journey, arrival handling and connection failures."""
import json

import pytest

from backend.agents import green_corridor as gc
from backend.agents.green_corridor import config
from backend.agents.green_corridor.connection import SumoConnection
from backend.agents.green_corridor.emergency_detector import (
    EmergencyDetector, detect_emergency_vehicle, get_emergency_status)
from .conftest import requires_sumo

pytestmark = requires_sumo


def _drive(sc, det, extra_steps=0):
    """Step until the ambulance has arrived; return the list of status payloads (one per step)."""
    samples, arrived_steps = [], 0
    while sc.traci.simulation.getTime() < 600:
        sc.step()
        r = get_emergency_status(det, "amb_1")
        if r["success"]:
            samples.append((r["data"], dict(speed=sc.traci.vehicle.getSpeed("amb_1"))
                            if "amb_1" in sc.traci.vehicle.getIDList() else None))
            if r["data"]["status"] == "ARRIVED":
                arrived_steps += 1
                if arrived_steps > extra_steps:
                    break
    return samples


def test_tracking_over_full_journey(sim):
    sc, det = sim
    samples = _drive(sc, det)
    states = [s for s, _ in samples]
    live = [s for s in states if s["status"] != "ARRIVED"]
    assert len(live) > 100, "journey should last well over 100 simulated seconds"

    # status progression
    assert live[0]["status"] == "DETECTED"
    assert all(s["status"] == "EN_ROUTE" for s in live[1:])
    assert states[-1]["status"] == "ARRIVED"

    # speed agrees with what SUMO reports directly
    for s, raw in samples:
        if raw:
            assert s["speed_mps"] == pytest.approx(raw["speed"], abs=0.01)
            assert s["speed_kmh"] == pytest.approx(raw["speed"] * 3.6, abs=0.02)

    # movement label is consistent with speed
    for s in live:
        assert (s["movement"] == "STOPPED") == (s["speed_mps"] < config.STOPPED_SPEED_MPS)

    # distance travelled never decreases; distance remaining never increases
    trav = [s["distance_travelled_m"] for s in live]
    rem = [s["remaining_distance_m"] for s in live]
    assert all(b >= a for a, b in zip(trav, trav[1:]))
    assert all(b <= a + 0.01 for a, b in zip(rem, rem[1:]))
    assert trav[-1] > 1500 and rem[-1] < 300

    # travelled + remaining stays ~constant (= route length), proving both are consistent
    totals = [t + r for t, r in zip(trav, rem)]
    assert max(totals) - min(totals) < 25

    # road ids follow the route in order; junction crossings are flagged
    route = live[0]["route_edges"]
    assert route[0] == "A0A1" and route[-1] == "E3E4" and live[0]["destination_edge"] == "E3E4"
    on_edges = [s["road_id"] for s in live if not s["on_junction"]]
    assert set(on_edges) <= set(route)
    idx = [route.index(e) for e in on_edges]
    assert idx == sorted(idx)
    assert any(s["on_junction"] and s["road_id"].startswith(":") for s in live)
    assert {"A0A1", "E3E4"} <= set(on_edges)


def test_trail_is_recorded(sim):
    sc, det = sim
    samples = _drive(sc, det)
    live_count = sum(1 for s, _ in samples if s["status"] != "ARRIVED")
    trail = det.get_track("amb_1")
    assert len(trail) == min(live_count, config.TRACK_HISTORY_LEN)
    times = [p["t"] for p in trail]
    assert times == sorted(times)
    assert trail[0]["x"] != trail[-1]["x"] or trail[0]["y"] != trail[-1]["y"]
    assert det.get_track("nobody") == []


def test_arrival_state_is_stable_and_no_longer_active(sim):
    sc, det = sim
    _drive(sc, det)
    for _ in range(5):
        sc.step()
        r = get_emergency_status(det, "amb_1")
        assert r["success"] and r["data"]["status"] == "ARRIVED" and r["data"]["speed_mps"] == 0
        assert r["data"]["movement"] == "STOPPED"
    assert detect_emergency_vehicle(det)["data"]["count"] == 0


def test_calling_update_twice_in_one_step_is_harmless(sim):
    sc, det = sim
    while not detect_emergency_vehicle(det)["data"]["count"]:
        sc.step()
    assert det.update() == {"new": [], "arrived": []}
    assert det.update() == {"new": [], "arrived": []}


def test_connection_loss_returns_error_envelope(sim):
    sc, det = sim
    while not detect_emergency_vehicle(det)["data"]["count"]:
        sc.step()
    sc.traci._process.kill()
    sc.traci._process.wait()
    r = get_emergency_status(det, "amb_1")
    assert r["success"] is False
    assert r["error"]["code"] == "CONNECTION_LOST"
    json.dumps(r)


def test_not_connected_returns_error_envelope():
    det = EmergencyDetector(SumoConnection())       # SUMO never started
    r = detect_emergency_vehicle(det)
    assert r["success"] is False and r["error"]["code"] == "NOT_CONNECTED"