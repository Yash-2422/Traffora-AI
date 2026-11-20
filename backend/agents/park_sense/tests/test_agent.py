"""Tests: the public ParkSense agent interface and analytics."""

import json
import shutil
from datetime import datetime

import pytest

from backend.agents.park_sense import config
from backend.agents.park_sense.agent import ParkSenseAgent
from backend.agents.park_sense.demand_predictor import DemandPredictor
from backend.agents.park_sense.schemas import InvalidParkingDataError

NOW = datetime(2026, 11, 24, 9, 30)
SAMPLE = {"PA_A": 5, "PA_B": 3, "PA_C": 2, "PA_D": 12, "PA_E": 1}


@pytest.fixture
def agent():
    # DemandPredictor(None) = rule-based only, so results never depend on a trained model
    a = ParkSenseAgent(predictor=DemandPredictor(None))
    a.update_occupancy(SAMPLE)
    return a


# ---------- the five public functions ----------
def test_get_parking_status(agent):
    status = agent.get_parking_status(NOW)
    assert status["timestamp"] == "2026-11-24T09:30:00"
    assert len(status["zones"]) == 5
    pa_a = next(z for z in status["zones"] if z["zone_id"] == "PA_A")
    assert (pa_a["capacity"], pa_a["occupied"], pa_a["available"]) == (6, 5, 1)
    assert pa_a["occupancy_pct"] == 83.3


def test_calculate_occupancy_for_zone(agent):
    assert agent.calculate_occupancy("PA_D")["occupancy_pct"] == 80.0


def test_calculate_occupancy_for_city(agent):
    assert agent.calculate_occupancy() == {
        "total_capacity": 51,
        "total_occupied": 23,
        "total_available": 28,
        "overall_occupancy_pct": 45.1,
    }


def test_predict_parking_demand(agent):
    result = agent.predict_parking_demand(NOW)
    assert result["horizon_minutes"] == config.PREDICTION_HORIZON_MINUTES
    assert len(result["predictions"]) == 5
    levels = {p["zone_id"]: p["demand_level"] for p in result["predictions"]}
    assert levels["PA_A"] == "High"
    assert levels["PA_E"] == "Low"


def test_recommend_parking(agent):
    result = agent.recommend_parking("hospital", now=NOW)
    assert result["best"] is not None
    assert result["destination"]["name"] == "City Hospital"
    scores = [r["score"] for r in result["recommendations"]]
    assert scores == sorted(scores, reverse=True)


def test_full_zone_is_never_recommended(agent):
    agent.update_occupancy({"PA_A": 6})
    result = agent.recommend_parking("hospital", top_n=5, now=NOW)
    assert "PA_A" in [e["zone_id"] for e in result["excluded"]]
    assert "PA_A" not in [r["zone_id"] for r in result["recommendations"]]


def test_get_parking_analytics(agent):
    a = agent.get_parking_analytics("hospital", now=NOW)
    assert a["total_capacity"] == 51
    assert a["total_occupied"] == 23
    assert a["total_available"] == 28
    assert a["overall_occupancy_pct"] == 45.1
    assert a["most_occupied_zone"]["zone_id"] == "PA_A"
    assert a["recommended_zone"]["zone_id"] == agent.recommend_parking("hospital", now=NOW)["best"]["zone_id"]
    assert a["demand_summary"] == {"Low": 3, "Medium": 0, "High": 2}
    assert len(a["zones"]) == 5


def test_every_public_output_is_json_serializable(agent):
    json.dumps(agent.get_parking_status(NOW))
    json.dumps(agent.calculate_occupancy("PA_A"))
    json.dumps(agent.calculate_occupancy())
    json.dumps(agent.predict_parking_demand(NOW))
    json.dumps(agent.recommend_parking("hospital", now=NOW))
    json.dumps(agent.get_parking_analytics("hospital", now=NOW))


# ---------- invalid data ----------
@pytest.mark.parametrize("bad_counts", [
    {"PA_A": 99},
    {"PA_A": -1},
    {"PA_A": 2.5},
    {"PA_A": True},
    {"PA_A": "3"},
    {"NOPE": 1},
    [("PA_A", 1)],
    None,
])
def test_update_occupancy_rejects_invalid_data(agent, bad_counts):
    with pytest.raises(InvalidParkingDataError):
        agent.update_occupancy(bad_counts)


def test_invalid_update_changes_nothing(agent):
    with pytest.raises(InvalidParkingDataError):
        agent.update_occupancy({"PA_B": 0, "PA_A": 99})
    assert agent.calculate_occupancy("PA_B")["occupied"] == 3  # unchanged


def test_unknown_zone_in_calculate_occupancy(agent):
    with pytest.raises(InvalidParkingDataError):
        agent.calculate_occupancy("NOPE")


def test_unknown_destination(agent):
    with pytest.raises(InvalidParkingDataError):
        agent.recommend_parking("atlantis", now=NOW)


# ---------- live SUMO (skipped automatically if SUMO is not installed) ----------
@pytest.mark.skipif(shutil.which("sumo") is None, reason="SUMO is not installed")
def test_live_sumo_data_is_valid():
    import sumolib
    import traci

    live_agent = ParkSenseAgent(predictor=DemandPredictor(None))
    traci.start([sumolib.checkBinary("sumo"), "-c", str(config.SUMO_CONFIG_FILE),
                 "--no-step-log", "true", "--no-warnings", "true"])
    try:
        while traci.simulation.getTime() < 300:
            traci.simulationStep()
            live_agent.update_from_sumo(traci.simulation.getTime())
    finally:
        traci.close()

    zones = live_agent.get_parking_status()["zones"]
    assert sum(z["occupied"] for z in zones) > 0
    assert all(0 <= z["occupied"] <= z["capacity"] for z in zones)