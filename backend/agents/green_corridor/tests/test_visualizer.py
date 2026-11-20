"""Tests for the GreenCorridor visualiser (Step 9). No SUMO required."""
import json

import numpy as np
import pytest

from backend.agents.green_corridor import visualizer as viz

NET_XML = """<net>
  <edge id=":A1_0" function="internal"><lane id=":A1_0_0" shape="99,0 101,0"/></edge>
  <edge id="A0A1"><lane id="A0A1_0" shape="0,0 100,0"/></edge>
  <edge id="A1A2"><lane id="A1A2_0" shape="100,0 200,0"/></edge>
  <junction id="A1" type="traffic_light" x="100" y="0"/>
  <junction id=":A1_0" type="internal" x="100" y="0"/>
</net>"""


def _metrics(journey, wait, kmh):
    return {
        "depart_time_s": 10.0, "arrival_time_s": 10.0 + journey,
        "journey_time_s": journey, "intersection_wait_time_s": wait, "stopped_time_s": wait,
        "average_speed_kmh": kmh, "distance_travelled_m": 200.0,
        "signalised_intersections_crossed": 1,
        "crossings": [{"tls_id": "A1", "approach_edge": "A0A1", "exit_edge": "A1A2",
                       "entered_time_s": 20.0 + wait, "crossed_time_s": 21.0 + wait,
                       "wait_time_s": wait, "speed_mps": 8.0}],
    }


@pytest.fixture
def results():
    return {
        "vehicle_id": "amb_1", "seed": 42, "completed_both": True,
        "baseline": {"metrics": _metrics(40.0, 5.0, 18.0)},
        "priority": {
            "metrics": _metrics(30.0, 0.0, 24.0),
            "priority": {
                "requests": 1, "by_state": {"RESTORED": 1},
                "signal_interventions": 3, "extensions": 1, "truncations": 1,
                "actions": [
                    {"time_s": 15.0, "call": "setPhaseDuration(A1, 20.5)",
                     "reason": "extend ambulance's own green so it does not turn before ETA"},
                    {"time_s": 16.0, "call": "setPhaseDuration(A1, 4.0)",
                     "reason": "truncate conflicting green to 4.0s"},
                    {"time_s": 21.0, "call": "setPhaseDuration(A1, 27.0)",
                     "reason": "restore this phase's normal programmed duration"},
                    {"time_s": 21.0, "call": "restore_normal_operation",
                     "reason": "ambulance has passed this intersection"},
                ],
            },
            "priority_errors": [],
        },
        "time_saved_s": 10.0, "time_saved_pct": 25.0,
    }


@pytest.fixture
def files(tmp_path, results):
    net = tmp_path / "test.net.xml"
    net.write_text(NET_XML, encoding="utf-8")
    res = tmp_path / "comparison.json"
    res.write_text(json.dumps(results), encoding="utf-8")
    return net, res, tmp_path / "out" / "dashboard.png"


def test_load_results_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        viz.load_results(tmp_path / "nope.json")


def test_load_results_bad_structure(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"baseline": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        viz.load_results(p)


def test_find_net_file_explicit_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        viz.find_net_file(tmp_path / "missing.net.xml")


def test_load_network_skips_internal_edges(files):
    net = viz.load_network(files[0])
    assert set(net["edges"]) == {"A0A1", "A1A2"}
    assert net["junctions"]["A1"] == (100.0, 0.0)
    assert ":A1_0" not in net["junctions"]


def test_route_edges_from_result(results):
    assert viz.route_edges_from_result(results) == ["A0A1", "A1A2"]


def test_route_geometry_rejects_unknown_edge(files):
    net = viz.load_network(files[0])
    with pytest.raises(ValueError):
        viz.build_route_geometry(net, ["A0A1", "ZZZ"])


def test_track_is_monotonic_and_reaches_end(files, results):
    net = viz.load_network(files[0])
    geom = viz.build_route_geometry(net, ["A0A1", "A1A2"])
    tr = viz.reconstruct_track(results["baseline"]["metrics"], geom)
    assert np.all(np.diff(tr["x"]) >= -1e-9)
    assert tr["elapsed"][0] == 0.0
    assert tr["x"][0] == pytest.approx(0.0)
    assert tr["x"][-1] == pytest.approx(200.0)


def test_priority_actions_classified(results):
    kinds = [(tls, k) for _, tls, k in viz.parse_priority_actions(results)]
    assert kinds == [("A1", "EXTEND"), ("A1", "TRUNCATE"), ("A1", "RESTORE")]


def test_main_writes_dashboard(files):
    net, res, out = files
    code = viz.main(["--results", str(res), "--net", str(net), "--out", str(out)])
    assert code == 0
    assert out.exists() and out.stat().st_size > 10_000


def test_main_returns_error_for_missing_results(files):
    net, _, out = files
    code = viz.main(["--results", str(out.parent / "none.json"), "--net", str(net),
                     "--out", str(out)])
    assert code == 1
    assert not out.exists()