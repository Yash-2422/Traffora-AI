"""Ambulance detection (testing requirement #1) + invalid input handling."""
import json

import pytest

from backend.agents.green_corridor.connection import SumoConnection
from backend.agents.green_corridor.emergency_detector import (
    EmergencyDetector, detect_emergency_vehicle, get_emergency_status)
from .conftest import requires_sumo


class _FakeTraci:
    """Minimal stand-in so classification logic can be tested without SUMO."""
    def __init__(self, vtype, vclass):
        self.vehicle = self
        self._t, self._c = vtype, vclass

    def getTypeID(self, v): return self._t
    def getVehicleClass(self, v): return self._c


def test_classification_rules():
    d = EmergencyDetector(_FakeTraci("car", "passenger"))
    assert not d.is_emergency("bg_1")                               # ordinary car
    assert EmergencyDetector(_FakeTraci("ambulance", "passenger")).is_emergency("x1")   # by vType id
    assert EmergencyDetector(_FakeTraci("truck", "emergency")).is_emergency("x1")       # by vClass
    assert d.is_emergency("amb_77")                                  # by id prefix
    assert not EmergencyDetector(_FakeTraci("car", "passenger"), id_prefixes=()).is_emergency("amb_77")


@requires_sumo
def test_nothing_detected_before_departure(sim):
    sc, det = sim
    for _ in range(5):
        sc.step()
    r = detect_emergency_vehicle(det)
    assert r["success"] and r["data"]["count"] == 0
    r = get_emergency_status(det, "amb_1")
    assert not r["success"] and r["error"]["code"] == "VEHICLE_NOT_FOUND"


@requires_sumo
def test_ambulance_detected_when_it_enters_simulation(sim):
    sc, det = sim
    r = {"data": {"count": 0}}
    while r["data"]["count"] == 0 and sc.traci.simulation.getTime() < 60:
        sc.step()
        r = detect_emergency_vehicle(det)
    assert r["success"] and r["data"]["vehicle_ids"] == ["amb_1"]
    v = r["data"]["vehicles"][0]
    assert v["vehicle_class"] == "emergency" and v["vehicle_type"] == "ambulance"
    assert v["destination_edge"] == "E3E4" and v["status"] == "DETECTED"
    assert 20 <= v["sim_time_s"] <= 22
    assert len(sc.traci.vehicle.getIDList()) > 1       # other traffic exists but was NOT flagged


@requires_sumo
def test_background_vehicle_is_rejected_as_non_emergency(sim):
    sc, det = sim
    bg = None
    while bg is None and sc.traci.simulation.getTime() < 60:
        sc.step()
        bg = next((v for v in sc.traci.vehicle.getIDList() if v.startswith("bg_")), None)
    assert bg is not None
    r = get_emergency_status(det, bg)
    assert not r["success"] and r["error"]["code"] == "NOT_EMERGENCY_VEHICLE"


@requires_sumo
@pytest.mark.parametrize("bad", ["", "   ", None, 5, ["amb_1"]])
def test_invalid_vehicle_id_rejected(sim, bad):
    _, det = sim
    r = get_emergency_status(det, bad)
    assert not r["success"] and r["error"]["code"] == "INVALID_INPUT"


@requires_sumo
@pytest.mark.parametrize("kwargs", [
    dict(id_prefixes=()),                                # detect by vType / vClass only
    dict(vtype_ids=(), vclass="none", id_prefixes=("amb_",)),   # detect by id prefix only
])
def test_each_identification_method_works_alone(scenario, kwargs):
    with SumoConnection() as sc:
        sc.start(scenario.baseline_cfg)
        det = EmergencyDetector(sc, **kwargs)
        found = []
        while not found and sc.traci.simulation.getTime() < 60:
            sc.step()
            found = detect_emergency_vehicle(det)["data"]["vehicle_ids"]
        assert found == ["amb_1"]


@requires_sumo
def test_outputs_are_json_serializable(sim):
    sc, det = sim
    while not detect_emergency_vehicle(det)["data"]["count"]:
        sc.step()
    for result in (detect_emergency_vehicle(det), get_emergency_status(det, "amb_1"),
                   get_emergency_status(det, "")):
        assert json.loads(json.dumps(result)) == result