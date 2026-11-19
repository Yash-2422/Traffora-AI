"""Step 3: scenario generation (network, ambulance vehicle type, origin/destination, configs)."""
import json
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from backend.agents.green_corridor import config
from backend.agents.green_corridor.connection import SumoConnection
from backend.agents.green_corridor.scenario_builder import ScenarioError, ScenarioSpec, build_scenario
from .conftest import requires_sumo

pytestmark = requires_sumo

SMALL = ScenarioSpec(background_vehicles=40, background_window_s=100, ambulance_depart_s=20, sim_end_s=600)


def _build(tmp_path, spec=SMALL, sub="s"):
    d = tmp_path / sub
    return build_scenario(spec, d / "net", d / "scn", d / "res")


def test_builds_all_files(tmp_path):
    f = _build(tmp_path)
    from pathlib import Path
    for p in (f.net_file, f.vtypes_file, f.background_routes_file, f.ambulance_routes_file,
              f.hospital_file, f.baseline_cfg, f.priority_cfg):
        assert Path(p).is_file(), p
    meta = json.loads((Path(f.baseline_cfg).parent / config.SCENARIO_META_FILENAME).read_text())
    assert meta["reference_route_edges"][0] == "A0A1" and meta["reference_route_edges"][-1] == "E3E4"
    assert meta["reference_route_length_m"] > 0


def test_network_has_signals_and_corners_unsignalised(tmp_path):
    f = _build(tmp_path)
    assert len(f.tls_ids) == 21          # 5x5 grid minus 4 corners
    assert not {"A0", "A4", "E0", "E4"} & set(f.tls_ids)


def test_ambulance_is_emergency_vtype(tmp_path):
    f = _build(tmp_path)
    vt = {v.get("id"): v for v in ET.parse(f.vtypes_file).getroot().iter("vType")}
    assert vt[config.AMBULANCE_VTYPE_ID].get("vClass") == "emergency"
    trip = next(ET.parse(f.ambulance_routes_file).getroot().iter("trip"))
    assert trip.get("type") == config.AMBULANCE_VTYPE_ID
    assert (trip.get("from"), trip.get("to")) == ("A0A1", "E3E4")


def test_same_seed_is_deterministic_and_different_seed_differs(tmp_path):
    a, b = _build(tmp_path, sub="a"), _build(tmp_path, sub="b")
    c = _build(tmp_path, replace(SMALL, seed=7), sub="c")
    from pathlib import Path
    read = lambda f: Path(f.background_routes_file).read_text()
    assert read(a) == read(b)
    assert read(a) != read(c)


def test_ambulance_endpoints_not_used_by_background(tmp_path):
    f = _build(tmp_path)
    for t in ET.parse(f.background_routes_file).getroot().iter("trip"):
        assert t.get("from") not in ("A0A1", "E3E4") and t.get("to") not in ("A0A1", "E3E4")


@pytest.mark.parametrize("bad", [
    dict(hospital_edge="Z9Z9"), dict(origin_edge="nope"), dict(grid_size=1),
    dict(origin_edge="E3E4", hospital_edge="E3E4"), dict(ambulance_depart_s=-1),
    dict(background_vehicles=-5), dict(sim_end_s=10, ambulance_depart_s=50),
])
def test_invalid_specs_rejected(tmp_path, bad):
    with pytest.raises(ScenarioError):
        _build(tmp_path, replace(SMALL, **bad))


def test_unknown_edge_error_code(tmp_path):
    with pytest.raises(ScenarioError) as e:
        _build(tmp_path, replace(SMALL, hospital_edge="Z9Z9"))
    assert e.value.code == "UNKNOWN_EDGE"


def test_ambulance_runs_in_sumo_and_reaches_hospital(tmp_path):
    """Real simulation: ambulance appears as an emergency vehicle and arrives at its destination."""
    f = _build(tmp_path)
    arrived_at = seen_class = None
    with SumoConnection() as sc:
        sc.start(f.baseline_cfg)
        t = sc.traci
        while t.simulation.getMinExpectedNumber() > 0 and t.simulation.getTime() < 600:
            sc.step()
            if seen_class is None and "amb_1" in t.vehicle.getIDList():
                seen_class = t.vehicle.getVehicleClass("amb_1")
                assert t.vehicle.getRoute("amb_1")[-1] == "E3E4"
            if "amb_1" in t.simulation.getArrivedIDList():
                arrived_at = t.simulation.getTime()
    assert seen_class == "emergency"
    assert arrived_at is not None and arrived_at > 20
    tripinfo = ET.parse(f.baseline_tripinfo).getroot()
    amb = next(x for x in tripinfo.iter("tripinfo") if x.get("id") == "amb_1")
    assert float(amb.get("duration")) > 0 and float(amb.get("routeLength")) > 1000