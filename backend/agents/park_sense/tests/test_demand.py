"""Tests: demand classification and demand prediction (rule-based and ML)."""

import json
from datetime import datetime

import pandas as pd
import pytest

from backend.agents.park_sense import config
from backend.agents.park_sense.demand_predictor import (
    DemandPredictor,
    classify_demand,
    load_history,
)
from backend.agents.park_sense.schemas import InvalidParkingDataError, ParkingZone, ZoneStatus

ZONE = ParkingZone("Z1", "Zone 1", 100, 0, 0)


def status(occupied):
    return ZoneStatus.build(ZONE, occupied)


def history(level_9, level_10):
    """A tiny weekday pattern: occupancy at 09:00 and at 10:00 on a Tuesday."""
    return pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-11-24 09:00", "2026-11-24 10:00"]),
        "zone_id": ["Z1", "Z1"],
        "occupancy_pct": [level_9, level_10],
    })


# ---------- classification ----------
@pytest.mark.parametrize("pct, expected", [
    (0, "Low"), (49.9, "Low"),
    (50, "Medium"), (79.9, "Medium"),
    (80, "High"), (100, "High"),
])
def test_classify_demand_levels(pct, expected):
    assert classify_demand(pct) == expected


@pytest.mark.parametrize("bad", [-1, 100.1, "high", None, float("nan"), True])
def test_classify_demand_rejects_invalid(bad):
    with pytest.raises(InvalidParkingDataError):
        classify_demand(bad)


# ---------- rule-based prediction ----------
def test_no_history_means_no_change():
    predictor = DemandPredictor(None, horizon_minutes=30)
    pred = predictor.predict([status(25)], datetime(2026, 11, 24, 9, 45))[0]
    assert pred.method == "rule_based"
    assert pred.predicted_occupancy_pct == 25.0
    assert pred.demand_level == "Low"


def test_rule_based_follows_usual_pattern():
    # usual level rises from 20% (09h) to 60% (10h), so 25% now -> 65% in 30 min
    predictor = DemandPredictor(history(20, 60), horizon_minutes=30)
    pred = predictor.predict([status(25)], datetime(2026, 11, 24, 9, 45))[0]
    assert pred.predicted_occupancy_pct == 65.0
    assert pred.demand_level == "Medium"
    assert pred.current_occupancy_pct == 25.0
    assert pred.horizon_minutes == 30


def test_rule_based_prediction_is_capped_at_100():
    predictor = DemandPredictor(history(20, 90), horizon_minutes=30)
    pred = predictor.predict([status(95)], datetime(2026, 11, 24, 9, 45))[0]
    assert pred.predicted_occupancy_pct == 100.0
    assert pred.demand_level == "High"


def test_rule_based_prediction_never_below_zero():
    predictor = DemandPredictor(history(90, 10), horizon_minutes=30)
    pred = predictor.predict([status(5)], datetime(2026, 11, 24, 9, 45))[0]
    assert pred.predicted_occupancy_pct == 0.0


def test_unknown_pattern_falls_back_to_current():
    # history only has a Tuesday; a Saturday has no pattern, so nothing changes
    predictor = DemandPredictor(history(20, 60), horizon_minutes=30)
    pred = predictor.predict([status(40)], datetime(2026, 11, 28, 9, 45))[0]
    assert pred.predicted_occupancy_pct == 40.0


def test_ml_requested_without_model_falls_back_to_rules():
    predictor = DemandPredictor(None)
    pred = predictor.predict([status(30)], datetime(2026, 11, 24, 9, 30), method="machine_learning")[0]
    assert pred.method == "rule_based"


def test_prediction_output_is_json_serializable():
    predictor = DemandPredictor(history(20, 60))
    pred = predictor.predict([status(25)], datetime(2026, 11, 24, 9, 45))[0]
    json.dumps(pred.to_dict())


def test_predict_rejects_empty_statuses():
    with pytest.raises(InvalidParkingDataError):
        DemandPredictor(None).predict([])


def test_predict_rejects_unknown_method():
    with pytest.raises(InvalidParkingDataError):
        DemandPredictor(None).predict([status(10)], method="magic")


# ---------- history loading and training checks ----------
def test_load_history_missing_file(tmp_path):
    with pytest.raises(InvalidParkingDataError):
        load_history(tmp_path / "nope.csv")


def test_load_history_missing_columns(tmp_path):
    f = tmp_path / "h.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(InvalidParkingDataError):
        load_history(f)


def test_load_history_drops_invalid_rows(tmp_path):
    f = tmp_path / "h.csv"
    pd.DataFrame({
        "timestamp": ["2026-11-24 09:00:00", "2026-11-24 09:15:00", "not a date"],
        "zone_id": ["Z1", "Z1", "Z1"],
        "capacity": [10, 10, 10],
        "occupied": [5, 10, 5],
        "occupancy_pct": [50.0, 150.0, 50.0],   # 150% and the bad date are invalid
    }).to_csv(f, index=False)
    assert len(load_history(f)) == 1


def test_training_needs_enough_history():
    predictor = DemandPredictor(history(20, 60))
    with pytest.raises(InvalidParkingDataError):
        predictor.train()


# ---------- machine learning (only runs after you trained the model) ----------
@pytest.mark.skipif(not config.MODEL_FILE.exists(), reason="Train the model first (--train)")
def test_ml_prediction_is_in_range():
    predictor = DemandPredictor.from_files()
    zone = ParkingZone("PA_A", "Hospital Annex Parking", 6, 0, 0)
    pred = predictor.predict(
        [ZoneStatus.build(zone, 5)], datetime(2026, 11, 24, 9, 30), method="machine_learning"
    )[0]
    assert pred.method == "machine_learning"
    assert 0 <= pred.predicted_occupancy_pct <= 100
    assert pred.demand_level in ("Low", "Medium", "High")