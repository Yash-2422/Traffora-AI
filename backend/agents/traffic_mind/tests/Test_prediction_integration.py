"""Prediction on a real SUMO run with the trained models (skipped without SUMO or models).

Checks the whole live path: simulation -> monitor -> detector -> predictor, and that the
live features are the ones the model was trained on (a mismatch would show as a clearly
worse forecast than "nothing changes").
"""

import json

import pytest

from backend.agents.traffic_mind import simulation_interface as si
from backend.agents.traffic_mind.config import MODEL_DIR, SimulationConfig
from backend.agents.traffic_mind.congestion_detector import CongestionDetector
from backend.agents.traffic_mind.simulation_interface import SimulationInterface
from backend.agents.traffic_mind.traffic_monitor import TrafficMonitor
from backend.agents.traffic_mind.traffic_predictor import TrafficPredictor

SCENARIO = SimulationConfig(scenario="surge", seed=42)
READY = (si.find_sumo_binary() is not None and SCENARIO.resolved_sumocfg().is_file()
         and (MODEL_DIR / "traffic_model_h60.joblib").is_file())
needs_sumo_and_model = pytest.mark.skipif(not READY, reason="SUMO, the surge scenario or the model is missing")


@needs_sumo_and_model
def test_live_forecasts_beat_no_change_on_a_real_run():
    predictor = TrafficPredictor()
    assert 60 in predictor.load_models()
    levels, forecasts, last = {}, [], None
    with SimulationInterface(SCENARIO) as sim:
        monitor = TrafficMonitor.from_source(sim)
        detector = CongestionDetector()
        while sim.current_time < 2000:
            sim.step(5)
            snapshot = monitor.sample(sim)
            last = predictor.predict(snapshot, detector.update(snapshot))
            t = int(round(last.timestamp_s))
            for p in last.predictions:
                levels[(p.junction_id, t)] = p.current_level.value
                if last.method == "ml":
                    forecasts.append((p.junction_id, t + 60, p.current_level.value, p.predicted_level.value))

    assert last.method == "ml" and last.model.horizon_s == 60 and len(last.predictions) == 9
    json.dumps(last.to_dict())
    scored = [(now, pred, levels[(j, t)]) for j, t, now, pred in forecasts if (j, t) in levels]
    assert len(scored) > 2000
    model_hits = sum(pred == actual for _, pred, actual in scored) / len(scored)
    stay_hits = sum(now == actual for now, _, actual in scored) / len(scored)
    assert model_hits > stay_hits + 0.03, (model_hits, stay_hits)


@needs_sumo_and_model
def test_first_minute_falls_back_to_persistence():
    predictor = TrafficPredictor()
    predictor.load_models()
    with SimulationInterface(SCENARIO) as sim:
        monitor = TrafficMonitor.from_source(sim)
        sim.step(30)
        report = predictor.predict(monitor.sample(sim))
    assert report.method == "persistence" and any("history" in n for n in report.notes)