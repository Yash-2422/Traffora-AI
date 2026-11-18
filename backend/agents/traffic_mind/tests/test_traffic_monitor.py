"""Tests for TrafficMonitor: metric calculations, flow, validation, SUMO integration."""

import json

import pytest

from backend.agents.traffic_mind import simulation_interface as si
from backend.agents.traffic_mind.config import MonitorConfig, SimulationConfig
from backend.agents.traffic_mind.exceptions import (
    InvalidTrafficDataError,
    SimulationNotRunningError,
)
from backend.agents.traffic_mind.schemas import (
    EdgeInfo,
    JunctionInfo,
    NetworkTopology,
    RawEdgeObservation,
    RawObservation,
    RawSignalObservation,
)
from backend.agents.traffic_mind.traffic_monitor import TrafficMonitor

# --------------------------------------------------------------------------- #
# Hand-made network:  E1, E3 -> junction J1 ;  E2 is a 2-lane exit road
# --------------------------------------------------------------------------- #
TOPOLOGY = NetworkTopology(
    edges=[
        EdgeInfo(edge_id="E1", length_m=200, lane_count=1, speed_limit_mps=10, junction_id="J1"),
        EdgeInfo(edge_id="E3", length_m=250, lane_count=1, speed_limit_mps=10, junction_id="J1"),
        EdgeInfo(edge_id="E2", length_m=500, lane_count=2, speed_limit_mps=20),
    ],
    junctions=[JunctionInfo(junction_id="J1", incoming_edges=["E1", "E3"], phase_count=4)],
)


def edge_obs(edge_id, ids=(), halting=0, occupancy=0.0, speed=10.0, wait_sum=0.0, queue_m=0.0):
    ids = list(ids)
    return RawEdgeObservation(
        edge_id=edge_id, vehicle_ids=ids, vehicle_count=len(ids), halting_count=halting,
        occupancy_pct=occupancy, mean_speed_mps=speed, waiting_time_sum_s=wait_sum,
        queue_length_m=queue_m,
    )


def observation(time_s, edges, total=None, signals=None):
    total = sum(e.vehicle_count for e in edges) if total is None else total
    return RawObservation(time_s=time_s, total_vehicles=total, edges=edges,
                          signals=signals or [])


def by_id(items, key):
    return {getattr(i, key): i for i in items}


@pytest.fixture
def monitor():
    return TrafficMonitor(TOPOLOGY)


# ------------------------------- calculations ------------------------------- #
def test_density_and_per_lane_density(monitor):
    snap = monitor.update(observation(10, [
        edge_obs("E1", ids=["a", "b", "c", "d", "e"]),                # 5 veh / 0.2 km
        edge_obs("E2", ids=[f"v{i}" for i in range(10)]),             # 10 veh / 0.5 km, 2 lanes
    ]))
    edges = by_id(snap.edges, "edge_id")
    assert edges["E1"].density_veh_per_km == 25.0
    assert edges["E2"].density_veh_per_km == 20.0
    assert edges["E2"].density_veh_per_km_lane == 10.0


def test_mean_waiting_time_is_per_vehicle(monitor):
    snap = monitor.update(observation(10, [
        edge_obs("E1", ids=["a", "b", "c", "d", "e"], wait_sum=60.0),
        edge_obs("E3"),  # empty road
    ]))
    edges = by_id(snap.edges, "edge_id")
    assert edges["E1"].mean_waiting_time_s == 12.0
    assert edges["E3"].mean_waiting_time_s == 0.0


def test_speed_ratio_is_capped_and_bounded(monitor):
    snap = monitor.update(observation(10, [
        edge_obs("E1", ids=["a"], speed=12.0),   # above limit (10) -> capped at 1
        edge_obs("E3", ids=["b"], speed=0.0),
        edge_obs("E2", ids=["c"], speed=10.0),   # limit 20 -> 0.5
    ]))
    edges = by_id(snap.edges, "edge_id")
    assert edges["E1"].speed_ratio == 1.0
    assert edges["E3"].speed_ratio == 0.0
    assert edges["E2"].speed_ratio == 0.5


def test_queue_and_occupancy_are_passed_through(monitor):
    snap = monitor.update(observation(10, [
        edge_obs("E1", ids=["a", "b", "c"], halting=3, occupancy=42.5, queue_m=22.5),
    ]))
    e1 = by_id(snap.edges, "edge_id")["E1"]
    assert (e1.queue_length_veh, e1.queue_length_m, e1.occupancy_pct) == (3, 22.5, 42.5)


def test_junction_aggregation(monitor):
    snap = monitor.update(observation(
        100,
        [
            edge_obs("E1", ids=["a", "b", "c", "d"], halting=3, occupancy=30.0,
                     wait_sum=40.0, queue_m=20.0),      # mean wait 10
            edge_obs("E3", ids=["e", "f"], halting=1, occupancy=55.0,
                     wait_sum=40.0, queue_m=8.0),       # mean wait 20
            edge_obs("E2", ids=["g"], halting=1, wait_sum=99.0, queue_m=99.0),  # not incoming
        ],
        signals=[RawSignalObservation(junction_id="J1", phase_index=3, phase_remaining_s=12.0)],
    ))
    j1 = by_id(snap.junctions, "junction_id")["J1"]
    assert j1.vehicle_count == 6
    assert j1.queue_length_veh == 4
    assert j1.max_queue_length_m == 20.0
    assert j1.max_occupancy_pct == 55.0
    assert j1.mean_waiting_time_s == pytest.approx((10 * 4 + 20 * 2) / 6, abs=0.01)
    assert (j1.current_phase, j1.phase_remaining_s) == (3, 12.0)
    assert snap.network.halting_vehicles == 5
    assert snap.network.total_vehicles == 7


def test_junction_without_signal_data_has_no_phase(monitor):
    snap = monitor.update(observation(5, [edge_obs("E1", ids=["a"])]))
    j1 = snap.junctions[0]
    assert j1.current_phase is None and j1.phase_remaining_s is None


def test_empty_network_is_handled(monitor):
    snap = monitor.update(observation(5, []))
    assert snap.edges == []
    assert snap.network.total_vehicles == 0
    assert snap.network.mean_speed_ratio == 1.0
    assert snap.junctions[0].vehicle_count == 0


# ----------------------------------- flow ----------------------------------- #
def test_flow_counts_vehicles_entering_the_road(monitor):
    first = monitor.update(observation(0, [edge_obs("E1", ids=["a", "b"])]))
    assert by_id(first.edges, "edge_id")["E1"].flow_veh_per_hour == 0.0  # no baseline yet

    second = monitor.update(observation(5, [edge_obs("E1", ids=["a", "b", "c", "d"])]))
    assert by_id(second.edges, "edge_id")["E1"].flow_veh_per_hour == 1440.0  # 2 in 5 s

    third = monitor.update(observation(10, [edge_obs("E1", ids=["c", "d", "e"])]))
    assert by_id(third.edges, "edge_id")["E1"].flow_veh_per_hour == 1080.0   # 3 in 10 s
    assert by_id(third.junctions, "junction_id")["J1"].flow_veh_per_hour == 1080.0


def test_flow_uses_a_rolling_window():
    monitor = TrafficMonitor(TOPOLOGY, MonitorConfig(flow_window_s=10))
    snap = None
    for step, t in enumerate(range(0, 25, 5)):
        snap = monitor.update(observation(t, [edge_obs("E1", ids=[f"v{step}"])]))
    # only the last two 5 s intervals (one new vehicle each) are inside the window
    assert by_id(snap.edges, "edge_id")["E1"].flow_veh_per_hour == 720.0


def test_time_going_backwards_resets_state(monitor):
    monitor.update(observation(100, [edge_obs("E1", ids=["a"])]))
    snap = monitor.update(observation(5, [edge_obs("E1", ids=["a", "b"])]))  # simulation restarted
    assert by_id(snap.edges, "edge_id")["E1"].flow_veh_per_hour == 0.0
    assert len(monitor.history) == 1


def test_history_is_limited():
    monitor = TrafficMonitor(TOPOLOGY, MonitorConfig(history_size=3))
    for t in range(5):
        monitor.update(observation(t, [edge_obs("E1")]))
    assert [s.timestamp_s for s in monitor.history] == [2, 3, 4]
    assert monitor.latest.timestamp_s == 4


# ------------------------------ invalid data -------------------------------- #
def good_edge_dict(**overrides):
    base = {"edge_id": "E1", "vehicle_ids": [], "vehicle_count": 0, "halting_count": 0,
            "occupancy_pct": 0.0, "mean_speed_mps": 5.0, "waiting_time_sum_s": 0.0,
            "queue_length_m": 0.0}
    base.update(overrides)
    return base


def dict_obs(**edge_overrides):
    return {"time_s": 10, "total_vehicles": 0, "edges": [good_edge_dict(**edge_overrides)]}


def test_dict_input_is_accepted(monitor):
    snap = monitor.update(dict_obs())
    assert snap.timestamp_s == 10


@pytest.mark.parametrize("bad", [
    {"vehicle_count": -1},
    {"occupancy_pct": 150.0},
    {"occupancy_pct": -1.0},
    {"mean_speed_mps": -3.0},
    {"waiting_time_sum_s": -1.0},
    {"vehicle_count": 2, "halting_count": 5},               # more halting than vehicles
    {"vehicle_count": 3, "vehicle_ids": ["a"]},             # ids do not match the count
    {"vehicle_count": "many"},
    {"unexpected_field": 1},                                 # typo in input
])
def test_invalid_edge_data_is_rejected(monitor, bad):
    with pytest.raises(InvalidTrafficDataError) as err:
        monitor.update(dict_obs(**bad))
    assert err.value.to_dict()["code"] == "INVALID_TRAFFIC_DATA"


def test_missing_fields_are_rejected(monitor):
    with pytest.raises(InvalidTrafficDataError):
        monitor.update({"edges": []})  # no time_s / total_vehicles


def test_unknown_road_is_rejected(monitor):
    with pytest.raises(InvalidTrafficDataError, match="not in the network"):
        monitor.update(dict_obs(edge_id="NOPE"))


def test_duplicate_road_is_rejected(monitor):
    with pytest.raises(InvalidTrafficDataError, match="Duplicate"):
        monitor.update(observation(1, [edge_obs("E1"), edge_obs("E1")]))


def test_unknown_junction_signal_is_rejected(monitor):
    with pytest.raises(InvalidTrafficDataError):
        monitor.update(observation(
            1, [edge_obs("E1")],
            signals=[RawSignalObservation(junction_id="X9", phase_index=0, phase_remaining_s=1)],
        ))


def test_invalid_topology_is_rejected():
    bad = {
        "edges": [{"edge_id": "E1", "length_m": 100, "lane_count": 1, "speed_limit_mps": 10}],
        "junctions": [{"junction_id": "J", "incoming_edges": ["MISSING"]}],
    }
    with pytest.raises(InvalidTrafficDataError):
        TrafficMonitor(bad)


def test_failed_update_leaves_monitor_usable(monitor):
    with pytest.raises(InvalidTrafficDataError):
        monitor.update(dict_obs(vehicle_count=-1))
    assert monitor.update(dict_obs()).timestamp_s == 10


# ---------------------------------- output ---------------------------------- #
def test_snapshot_is_json_serializable(monitor):
    snap = monitor.update(observation(
        10, [edge_obs("E1", ids=["a", "b"], halting=1, wait_sum=8.0)],
        signals=[RawSignalObservation(junction_id="J1", phase_index=0, phase_remaining_s=7.5)],
    ))
    data = json.loads(json.dumps(snap.to_dict()))
    assert data["status"] == "ok"
    assert set(data) == {"status", "timestamp_s", "network", "junctions", "edges"}
    assert data["junctions"][0]["junction_id"] == "J1"


def test_from_source_and_sample():
    class FakeSource:
        def read_topology(self):
            return TOPOLOGY

        def read_observation(self):
            return observation(20, [edge_obs("E1", ids=["a"])])

    source = FakeSource()
    monitor = TrafficMonitor.from_source(source)
    assert monitor.sample(source).timestamp_s == 20


def test_reading_from_a_stopped_simulation_raises():
    sim = si.SimulationInterface(SimulationConfig())
    with pytest.raises(SimulationNotRunningError):
        sim.read_observation()
    with pytest.raises(SimulationNotRunningError):
        sim.read_topology()


# ------------------------------- needs SUMO --------------------------------- #
SCENARIO = SimulationConfig(scenario="mixed")
SUMO_READY = si.find_sumo_binary() is not None and SCENARIO.resolved_sumocfg().is_file()
needs_sumo = pytest.mark.skipif(
    not SUMO_READY,
    reason="SUMO or the built scenario is missing "
           "(run: python simulation/traffic_mind/build_scenario.py)",
)


@needs_sumo
def test_topology_of_the_sample_network():
    with si.SimulationInterface(SCENARIO) as sim:
        topology = sim.read_topology()
    assert len(topology.edges) == 48
    assert len(topology.junctions) == 9
    assert all(len(j.incoming_edges) == 4 for j in topology.junctions)
    junction_ids = {j.junction_id for j in topology.junctions}
    assert {e.junction_id for e in topology.edges if e.junction_id} == junction_ids


@needs_sumo
def test_live_snapshot_is_consistent():
    with si.SimulationInterface(SCENARIO) as sim:
        monitor = TrafficMonitor.from_source(sim)
        snap = None
        for _ in range(60):          # 300 s sampled every 5 s
            sim.step(5)
            snap = monitor.sample(sim)

    assert snap.timestamp_s == 300.0
    assert snap.network.total_vehicles > 0
    assert sum(e.vehicle_count for e in snap.edges) <= snap.network.total_vehicles
    assert snap.network.halting_vehicles <= snap.network.total_vehicles
    assert any(e.flow_veh_per_hour > 0 for e in snap.edges)

    edges = by_id(snap.edges, "edge_id")
    for e in snap.edges:
        assert 0 <= e.occupancy_pct <= 100
        assert 0 <= e.speed_ratio <= 1
        assert e.queue_length_m >= 0 and e.density_veh_per_km >= 0
        assert e.queue_length_m <= e.length_m
    for j in snap.junctions:
        incoming = [edges[e] for e in j.incoming_edges]
        assert j.vehicle_count == sum(e.vehicle_count for e in incoming)
        assert j.queue_length_veh == sum(e.queue_length_veh for e in incoming)
        assert j.current_phase is not None and j.phase_remaining_s >= 0
    json.dumps(snap.to_dict())


@needs_sumo
def test_occupancy_is_a_percentage_that_matches_vehicle_count():
    """Guards the unit: occupancy ~ (vehicles x 5 m car length) / road length, in percent."""
    with si.SimulationInterface(SCENARIO) as sim:
        monitor = TrafficMonitor.from_source(sim)
        sim.step(1200)
        snap = monitor.sample(sim)
    crowded = [e for e in snap.edges if e.vehicle_count >= 8]
    assert crowded, "scenario should have crowded roads at t=1200 s"
    for e in crowded:
        expected = e.vehicle_count * 5.0 / e.length_m * 100.0
        assert abs(e.occupancy_pct - expected) <= 10.0, (e.edge_id, e.occupancy_pct, expected)
        assert e.occupancy_pct > 10.0  # would be < 1 if SUMO's fraction was left unconverted


@needs_sumo
def test_monitor_sees_congestion_build_up():
    """Peak demand (from t=600 s) must show more queued vehicles than the quiet start."""
    with si.SimulationInterface(SCENARIO) as sim:
        monitor = TrafficMonitor.from_source(sim)
        sim.step(300)
        quiet = monitor.sample(sim)
        sim.step(1000)
        busy = monitor.sample(sim)
    assert busy.network.total_vehicles > quiet.network.total_vehicles
    assert busy.network.halting_vehicles > quiet.network.halting_vehicles