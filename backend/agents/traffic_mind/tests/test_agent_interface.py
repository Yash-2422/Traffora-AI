"""The four public functions: JSON in, JSON out, no exceptions, one sample per time."""

import json

import pytest

from backend.agents.traffic_mind import agent as ag
from backend.agents.traffic_mind.config import REPO_ROOT
from backend.agents.traffic_mind.exceptions import InvalidTrafficDataError, SimulationNotRunningError
from backend.agents.traffic_mind.schemas import (
    CongestionReport,
    EdgeMetrics,
    JunctionMetrics,
    NetworkMetrics,
    PredictionReport,
    SignalRecommendationReport,
    TrafficSnapshot,
)
from backend.agents.traffic_mind.simulation_interface import SimulationInterface
from backend.agents.traffic_mind.config import SimulationConfig
from backend.agents.traffic_mind import simulation_interface as si

EXAMPLES = REPO_ROOT / "docs" / "traffic_mind" / "examples"
LENGTH = 200.0
EDGES = ("A", "B", "C", "D")
PROGRAM = {
    "junction_id": "J1",
    "link_edges": list(EDGES),
    "phases": [
        {"state": "GGrr", "duration_s": 30},
        {"state": "yyrr", "duration_s": 3},
        {"state": "rrrr", "duration_s": 2},
        {"state": "rrGG", "duration_s": 30},
        {"state": "rryy", "duration_s": 3},
        {"state": "rrrr", "duration_s": 2},
    ],
}

SUMO_READY = si.find_sumo_binary() is not None and SimulationConfig(scenario="offpeak").resolved_sumocfg().is_file()
needs_sumo = pytest.mark.skipif(not SUMO_READY, reason="SUMO or the offpeak scenario is missing")


@pytest.fixture(autouse=True)
def _fresh():
    ag.reset_agent()
    yield
    ag.reset_agent()


def road(edge_id, queue_veh):
    vehicles = queue_veh + 2
    return EdgeMetrics(
        edge_id=edge_id, junction_id="J1", length_m=LENGTH, lane_count=1, speed_limit_mps=10.0,
        vehicle_count=vehicles, density_veh_per_km=vehicles * 5.0, density_veh_per_km_lane=vehicles * 5.0,
        occupancy_pct=min(100.0, 5.0 + 4.0 * queue_veh), queue_length_veh=queue_veh,
        queue_length_m=min(LENGTH, 7.5 * queue_veh), mean_waiting_time_s=3.0 * queue_veh,
        mean_speed_mps=10.0 * max(0.0, 1.0 - 0.1 * queue_veh), speed_ratio=max(0.0, 1.0 - 0.1 * queue_veh),
        flow_veh_per_hour=300.0,
    )


def snapshot(queues, t, phase=0, remaining=20.0):
    edges = [road(edge_id, queues.get(edge_id, 0)) for edge_id in EDGES]
    junction = JunctionMetrics(
        junction_id="J1", incoming_edges=list(EDGES),
        vehicle_count=sum(edge.vehicle_count for edge in edges),
        queue_length_veh=sum(edge.queue_length_veh for edge in edges),
        max_queue_length_m=max(edge.queue_length_m for edge in edges),
        mean_waiting_time_s=max(edge.mean_waiting_time_s for edge in edges),
        max_occupancy_pct=max(edge.occupancy_pct for edge in edges),
        flow_veh_per_hour=1200.0, current_phase=phase, phase_remaining_s=remaining,
    )
    return TrafficSnapshot(
        timestamp_s=t,
        network=NetworkMetrics(
            total_vehicles=sum(edge.vehicle_count for edge in edges),
            halting_vehicles=sum(edge.queue_length_veh for edge in edges),
            mean_waiting_time_s=1.0, mean_speed_ratio=0.8,
        ),
        junctions=[junction], edges=edges,
    ).to_dict()


def test_public_names_match_the_brief():
    assert ag.__all__ == [
        "TrafficMindAgent",
        "attach_simulation",
        "load_signal_programs",
        "get_traffic_status",
        "detect_congestion",
        "predict_congestion",
        "generate_signal_recommendation",
        "get_agent",
        "reset_agent",
    ]


@pytest.mark.parametrize("fn", [
    ag.get_traffic_status, ag.detect_congestion, ag.predict_congestion,
])
def test_garbage_snapshot_is_an_error_object(fn):
    out = fn({"garbage": True})
    json.dumps(out)
    assert out["status"] == "error"
    assert out["code"] == "INVALID_TRAFFIC_DATA"
    assert out["message"]


def test_signal_advice_without_programs_is_an_error_object():
    out = ag.generate_signal_recommendation(snapshot({"A": 8, "B": 8}, t=100))
    json.dumps(out)
    assert out == {
        "status": "error",
        "code": "SIMULATION_NOT_RUNNING",
        "message": out["message"],
    }
    assert "load_signal_programs" in out["message"]


def test_no_simulation_and_no_snapshot_is_an_error_object():
    for fn in (ag.get_traffic_status, ag.detect_congestion, ag.predict_congestion):
        out = fn()
        assert out["status"] == "error" and out["code"] == "SIMULATION_NOT_RUNNING"


def test_bad_horizon_is_an_error_and_does_not_require_data():
    out = ag.predict_congestion(horizon_s=0)
    assert out["code"] == "PREDICTION_ERROR"
    assert ag.predict_congestion(horizon_s=True)["code"] == "PREDICTION_ERROR"


def test_attach_requires_a_running_simulation():
    with pytest.raises(SimulationNotRunningError):
        ag.attach_simulation(SimulationInterface())


def test_empty_programs_are_rejected():
    with pytest.raises(InvalidTrafficDataError):
        ag.load_signal_programs({})


def test_recorded_status_round_trips_and_feeds_the_later_functions():
    recorded = json.loads((EXAMPLES / "traffic_status.json").read_text(encoding="utf-8"))
    status = ag.get_traffic_status(recorded)
    congestion = ag.detect_congestion()
    forecast = ag.predict_congestion()
    json.dumps(status)
    json.dumps(congestion)
    json.dumps(forecast)
    assert status == recorded
    CongestionReport.model_validate(congestion)
    PredictionReport.model_validate(forecast)
    assert congestion["timestamp_s"] == forecast["timestamp_s"] == 1800.0
    assert congestion["summary"]["junctions_high"] + congestion["summary"]["junctions_medium"] >= 1
    # one sample is not enough history for the trained model
    assert forecast["method"] == "persistence"
    assert forecast["horizon_s"] == 60


def test_pipeline_is_one_sample_and_signal_advice_is_only_a_recommendation():
    ag.load_signal_programs({"J1": PROGRAM})
    first = snapshot({"A": 8, "B": 8, "C": 0, "D": 0}, t=100)
    status = ag.get_traffic_status(first)
    congestion = ag.detect_congestion()
    forecast = ag.predict_congestion(horizon_s=60)
    advice = ag.generate_signal_recommendation()
    assert status["timestamp_s"] == congestion["timestamp_s"] == forecast["timestamp_s"] == advice["timestamp_s"]
    SignalRecommendationReport.model_validate(advice)
    assert advice["mode"] == "recommend_only"
    assert advice["recommendations"][0]["safety"]["validated"] is True
    assert advice["recommendations"][0]["action"] == "rebalance"

    again = ag.predict_congestion()
    assert again == forecast
    assert len(ag.get_agent().predictor._builder._history["J1"]) == 1

    later = snapshot({"A": 8, "B": 8, "C": 0, "D": 0}, t=105)
    second = ag.predict_congestion(later, horizon_s=30)
    assert second["horizon_s"] == 30
    assert second["history_available_s"] == 5
    assert len(ag.get_agent().predictor._builder._history["J1"]) == 2
    ag.predict_congestion(horizon_s=60)
    assert len(ag.get_agent().predictor._builder._history["J1"]) == 2


def test_same_snapshot_is_not_scored_twice():
    sample = snapshot({"A": 1}, t=50)
    first = ag.detect_congestion(sample)
    second = ag.detect_congestion(sample)
    assert first == second
    assert len(ag.get_agent().detector._states) == (
        len(first["edges"]) + len(first["junctions"])
    )


@needs_sumo
def test_live_simulation_runs_the_four_functions():
    sim = SimulationInterface(SimulationConfig(scenario="offpeak", label="agent_interface"))
    sim.start()
    try:
        ag.attach_simulation(sim)
        sim.step(5)
        status = ag.get_traffic_status()
        congestion = ag.detect_congestion()
        forecast = ag.predict_congestion()
        advice = ag.generate_signal_recommendation()
        assert status["status"] == congestion["status"] == forecast["status"] == advice["status"] == "ok"
        assert advice["mode"] == "recommend_only"
        assert status["timestamp_s"] == advice["timestamp_s"] == sim.current_time
        assert ag.get_traffic_status()["timestamp_s"] == status["timestamp_s"]
        assert len(advice["recommendations"]) == 9
    finally:
        sim.close()
