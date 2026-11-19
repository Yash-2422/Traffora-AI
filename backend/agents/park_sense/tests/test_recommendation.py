"""Tests: parking recommendation, distance-based ranking, unavailable zones, invalid input."""

import json

import pytest

from backend.agents.park_sense import config
from backend.agents.park_sense.recommendation_engine import recommend_parking, resolve_destination
from backend.agents.park_sense.schemas import (
    DemandPrediction,
    InvalidParkingDataError,
    ParkingZone,
    ZoneStatus,
)


def make_case(specs, levels=None):
    """specs = [(zone_id, capacity, occupied, x, y)]. levels = {zone_id: "Low"/"Medium"/"High"}."""
    zones, statuses, preds = [], [], []
    for zone_id, capacity, occupied, x, y in specs:
        zone = ParkingZone(zone_id, f"Parking {zone_id}", capacity, x, y)
        stat = ZoneStatus.build(zone, occupied)
        level = (levels or {}).get(zone_id, "Medium")
        zones.append(zone)
        statuses.append(stat)
        preds.append(DemandPrediction(zone_id, stat.occupancy_pct, stat.occupancy_pct, level, "rule_based", 30))
    return zones, statuses, preds


# ---------- the hospital example from the project brief ----------
def hospital_case():
    # A: 2 free, 1000 m | B: 12 free, 1500 m | C: 0 free, 500 m
    return make_case([("A", 10, 8, 1000, 0), ("B", 20, 8, 1500, 0), ("C", 10, 10, 500, 0)])


def test_hospital_example_best_is_not_the_closest():
    zones, statuses, preds = hospital_case()
    result = recommend_parking(zones, statuses, preds, (0, 0))
    assert result["best"]["zone_id"] == "B"
    assert [r["zone_id"] for r in result["recommendations"]] == ["B", "A"]


def test_hospital_example_scores():
    zones, statuses, preds = hospital_case()
    recs = {r["zone_id"]: r for r in recommend_parking(zones, statuses, preds, (0, 0))["recommendations"]}
    assert recs["B"]["score"] == pytest.approx(0.575, abs=0.001)
    assert recs["A"]["score"] == pytest.approx(0.395, abs=0.001)


def test_full_zone_is_excluded_with_reason():
    zones, statuses, preds = hospital_case()
    result = recommend_parking(zones, statuses, preds, (0, 0))
    assert [e["zone_id"] for e in result["excluded"]] == ["C"]
    assert result["excluded"][0]["reason"] == "No free spaces"


# ---------- ranking behaviour ----------
def test_distance_based_ranking():
    # identical zones except for distance: nearest must rank first
    zones, statuses, preds = make_case([
        ("N1", 10, 0, 600, 0), ("N2", 10, 0, 100, 0), ("N3", 10, 0, 300, 0),
    ])
    result = recommend_parking(zones, statuses, preds, (0, 0))
    recs = result["recommendations"]
    assert [r["zone_id"] for r in recs] == ["N2", "N3", "N1"]
    assert [r["distance_m"] for r in recs] == [100.0, 300.0, 600.0]
    assert recs[0]["eta_seconds"] < recs[1]["eta_seconds"] < recs[2]["eta_seconds"]


def test_more_free_spaces_wins_at_equal_distance():
    zones, statuses, preds = make_case([("F", 10, 9, 300, 0), ("M", 10, 0, 0, 300)])
    assert recommend_parking(zones, statuses, preds, (0, 0))["best"]["zone_id"] == "M"


def test_low_demand_beats_high_demand_at_equal_distance_and_space():
    zones, statuses, preds = make_case(
        [("L", 10, 0, 300, 0), ("H", 10, 0, 0, 300)], levels={"L": "Low", "H": "High"}
    )
    assert recommend_parking(zones, statuses, preds, (0, 0))["best"]["zone_id"] == "L"


def test_top_n_limits_results_and_ranks_are_sequential():
    zones, statuses, preds = make_case([(f"Z{i}", 10, 0, 100 * (i + 1), 0) for i in range(5)])
    result = recommend_parking(zones, statuses, preds, (0, 0), top_n=2)
    assert [r["rank"] for r in result["recommendations"]] == [1, 2]


def test_reason_text_is_generated():
    zones, statuses, preds = hospital_case()
    best = recommend_parking(zones, statuses, preds, (0, 0))["best"]
    assert "free spaces" in best["reason"]


def test_missing_prediction_gives_unknown_demand():
    zones, statuses, _ = make_case([("A", 10, 0, 100, 0)])
    result = recommend_parking(zones, statuses, None, (0, 0))
    assert result["best"]["demand_level"] == "Unknown"


# ---------- unavailable parking ----------
def test_all_zones_full():
    zones, statuses, preds = make_case([("A", 5, 5, 100, 0), ("B", 5, 5, 200, 0)])
    result = recommend_parking(zones, statuses, preds, (0, 0))
    assert result["best"] is None
    assert result["recommendations"] == []
    assert len(result["excluded"]) == 2
    assert result["message"] == "No parking zone currently has free spaces"


def test_zone_without_location_is_excluded():
    zones, statuses, preds = make_case([("A", 10, 0, 100, 0)])
    ghost = ZoneStatus.build(ParkingZone("G", "Ghost", 5, 0, 0), 0)
    result = recommend_parking(zones, statuses + [ghost], preds, (0, 0))
    assert [e["zone_id"] for e in result["excluded"]] == ["G"]
    assert result["best"]["zone_id"] == "A"


# ---------- destinations ----------
def test_named_destination_from_zones_file():
    dest = resolve_destination("Hospital")
    assert (dest["x"], dest["y"]) == (400.0, 200.0)


def test_dict_and_tuple_destinations():
    assert resolve_destination({"x": 5, "y": 6, "name": "Clinic"})["name"] == "Clinic"
    assert resolve_destination((5, 6))["y"] == 6.0


# ---------- invalid input ----------
@pytest.mark.parametrize("bad", [
    "atlantis",
    {"x": "a", "y": 1},
    {"x": 1},
    {"x": True, "y": 1},
    (1, 2, 3),
    (float("nan"), 1),
    None,
    42,
])
def test_invalid_destination_raises(bad):
    with pytest.raises(InvalidParkingDataError):
        resolve_destination(bad)


def test_empty_statuses_raises():
    with pytest.raises(InvalidParkingDataError):
        recommend_parking([], [], [], (0, 0))


@pytest.mark.parametrize("bad_top_n", [0, -1, 1.5, "3", True])
def test_invalid_top_n_raises(bad_top_n):
    zones, statuses, preds = hospital_case()
    with pytest.raises(InvalidParkingDataError):
        recommend_parking(zones, statuses, preds, (0, 0), top_n=bad_top_n)


def test_weights_must_add_up_to_one(monkeypatch):
    zones, statuses, preds = hospital_case()
    monkeypatch.setattr(config, "WEIGHT_DISTANCE", 0.9)
    with pytest.raises(InvalidParkingDataError):
        recommend_parking(zones, statuses, preds, (0, 0))


def test_result_is_json_serializable():
    zones, statuses, preds = hospital_case()
    json.dumps(recommend_parking(zones, statuses, preds, (0, 0)))