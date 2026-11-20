"""ParkSense recommendation engine.

Ranks parking zones using distance, free spaces and predicted demand.

Run the demo from the project root:
    python -m backend.agents.park_sense.recommendation_engine --demo
"""

import argparse
import json
import logging
import math
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Union

from . import config
from .schemas import (
    DemandPrediction,
    InvalidParkingDataError,
    ParkingRecommendation,
    ParkingZone,
    ZoneStatus,
)

logger = logging.getLogger(__name__)

DETOUR_FACTOR = 1.3              # roads are longer than straight lines (used for travel time only)
MAX_DISTANCE_M = 2000.0          # at or beyond this distance, the distance score is 0
AVAILABILITY_SATURATION = 10     # this many free spaces or more gives the full availability score
DEMAND_SCORES = {"Low": 1.0, "Medium": 0.5, "High": 0.0}
UNKNOWN_DEMAND_SCORE = 0.5       # used when no prediction exists for a zone


# ---------- destination handling ----------
def _check_number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidParkingDataError(f"Destination {name} must be a finite number, got {value!r}")
    return float(value)


def resolve_destination(destination: Union[str, dict, Sequence[float]], zones_path=None) -> dict:
    """Turn a destination into {"name", "x", "y"}.

    Accepted input:
      "hospital"                       - a name from the destinations in zones.json
      {"x": 400, "y": 200}             - coordinates (optional "name")
      (400, 200)                       - coordinates as a tuple or list
    """
    if isinstance(destination, str):
        path = zones_path or config.ZONES_FILE
        try:
            with open(path, "r", encoding="utf-8") as f:
                known = json.load(f).get("destinations", {})
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise InvalidParkingDataError(f"Could not read destinations: {exc}") from exc
        key = destination.strip().lower()
        if key not in known:
            raise InvalidParkingDataError(
                f"Unknown destination {destination!r}. Known: {sorted(known)}"
            )
        entry = known[key]
        return {
            "name": entry.get("name", key),
            "x": _check_number(entry.get("x"), "x"),
            "y": _check_number(entry.get("y"), "y"),
        }
    if isinstance(destination, dict):
        return {
            "name": str(destination.get("name", "custom destination")),
            "x": _check_number(destination.get("x"), "x"),
            "y": _check_number(destination.get("y"), "y"),
        }
    if isinstance(destination, (list, tuple)) and len(destination) == 2:
        return {
            "name": "custom destination",
            "x": _check_number(destination[0], "x"),
            "y": _check_number(destination[1], "y"),
        }
    raise InvalidParkingDataError(f"Unsupported destination: {destination!r}")


# ---------- scoring helpers ----------
def _check_weights() -> None:
    total = config.WEIGHT_AVAILABILITY + config.WEIGHT_DISTANCE + config.WEIGHT_DEMAND
    if abs(total - 1.0) > 1e-6:
        raise InvalidParkingDataError(f"Recommendation weights must add up to 1.0, got {total}")


def _distance_score(distance_m: float) -> float:
    return max(0.0, 1.0 - distance_m / MAX_DISTANCE_M)


def _availability_score(available: int) -> float:
    return min(available, AVAILABILITY_SATURATION) / AVAILABILITY_SATURATION


def _demand_score(level: Optional[str]) -> float:
    return DEMAND_SCORES.get(level, UNKNOWN_DEMAND_SCORE)


def _build_reason(item: dict, nearest_id: str, most_spaces_id: str) -> str:
    parts = []
    if item["zone"].zone_id == nearest_id:
        parts.append("nearest zone with free space")
    if item["zone"].zone_id == most_spaces_id:
        parts.append(f"most free spaces ({item['available']})")
    elif item["available"] <= 2:
        parts.append(f"only {item['available']} space(s) left")
    else:
        parts.append(f"{item['available']} free spaces")
    level = item["demand_level"]
    if level == "Low":
        parts.append("demand expected to stay low")
    elif level == "High":
        parts.append("demand expected to be high, may fill soon")
    elif level == "Medium":
        parts.append("moderate demand expected")
    else:
        parts.append("no demand prediction available")
    return "; ".join(parts)


# ---------- main function ----------
def recommend_parking(
    zones: List[ParkingZone],
    statuses: List[ZoneStatus],
    predictions: Optional[List[DemandPrediction]],
    destination: Union[str, dict, Sequence[float]],
    top_n: int = 3,
) -> dict:
    """Rank parking zones for a driver going to `destination`.

    Input:
      zones        - ParkingZone list (locations)
      statuses     - ZoneStatus list (live occupancy)
      predictions  - DemandPrediction list (may be empty or None)
      destination  - "hospital", {"x":..,"y":..} or (x, y)
      top_n        - how many ranked options to return
    Output (JSON-serializable dict):
      {
        "destination": {"name", "x", "y"},
        "recommendations": [ParkingRecommendation dicts, best first],
        "best": ParkingRecommendation dict or None,
        "excluded": [{"zone_id", "name", "reason"}],
        "message": str
      }
    """
    _check_weights()
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise InvalidParkingDataError(f"top_n must be a positive integer, got {top_n!r}")
    if not statuses:
        raise InvalidParkingDataError("No zone statuses were given to recommend_parking")

    dest = resolve_destination(destination)
    zone_map: Dict[str, ParkingZone] = {z.zone_id: z for z in zones}
    pred_map: Dict[str, DemandPrediction] = {p.zone_id: p for p in (predictions or [])}

    candidates, excluded = [], []
    for status in statuses:
        zone = zone_map.get(status.zone_id)
        if zone is None:
            logger.warning("No location known for zone %s; skipping", status.zone_id)
            excluded.append({"zone_id": status.zone_id, "name": status.name,
                             "reason": "No location data for this zone"})
            continue
        if status.available < config.MIN_AVAILABLE_SPACES:
            excluded.append({"zone_id": status.zone_id, "name": status.name,
                             "reason": "No free spaces" if status.available == 0
                             else f"Fewer than {config.MIN_AVAILABLE_SPACES} free spaces"})
            continue

        distance = math.hypot(zone.x - dest["x"], zone.y - dest["y"])
        eta = distance * DETOUR_FACTOR / config.AVERAGE_SPEED_MPS
        prediction = pred_map.get(status.zone_id)
        level = prediction.demand_level if prediction else None

        score = (
            config.WEIGHT_DISTANCE * _distance_score(distance)
            + config.WEIGHT_AVAILABILITY * _availability_score(status.available)
            + config.WEIGHT_DEMAND * _demand_score(level)
        )
        candidates.append({
            "zone": zone, "available": status.available, "distance": distance,
            "eta": eta, "demand_level": level, "score": score,
        })

    if not candidates:
        return {
            "destination": dest,
            "recommendations": [],
            "best": None,
            "excluded": excluded,
            "message": "No parking zone currently has free spaces",
        }

    candidates.sort(key=lambda c: (-c["score"], c["distance"], c["zone"].zone_id))
    nearest_id = min(candidates, key=lambda c: c["distance"])["zone"].zone_id
    most_spaces_id = max(candidates, key=lambda c: c["available"])["zone"].zone_id

    recommendations = []
    for rank, item in enumerate(candidates[:top_n], start=1):
        recommendations.append(ParkingRecommendation(
            rank=rank,
            zone_id=item["zone"].zone_id,
            name=item["zone"].name,
            available_spaces=item["available"],
            distance_m=round(item["distance"], 1),
            eta_seconds=round(item["eta"], 1),
            demand_level=item["demand_level"] or "Unknown",
            score=round(item["score"], 3),
            reason=_build_reason(item, nearest_id, most_spaces_id),
        ).to_dict())

    best = recommendations[0]
    return {
        "destination": dest,
        "recommendations": recommendations,
        "best": best,
        "excluded": excluded,
        "message": f"Recommended: {best['name']} ({best['zone_id']})",
    }


# ---------- demo ----------
def _print_result(result: dict) -> None:
    print(f"Destination: {result['destination']['name']}")
    print(f"{'Rank':<5}{'Zone':<7}{'Free':>5}{'Dist m':>9}{'ETA s':>8}{'Demand':>9}{'Score':>8}  Reason")
    for r in result["recommendations"]:
        print(f"{r['rank']:<5}{r['zone_id']:<7}{r['available_spaces']:>5}{r['distance_m']:>9}"
              f"{r['eta_seconds']:>8}{r['demand_level']:>9}{r['score']:>8}  {r['reason']}")
    for e in result["excluded"]:
        print(f"  excluded: {e['zone_id']} ({e['reason']})")
    print(f"=> {result['message']}\n")


def _prediction(zone_id: str, pct: float, level: str) -> DemandPrediction:
    return DemandPrediction(zone_id, pct, pct, level, "rule_based", config.PREDICTION_HORIZON_MINUTES)


def run_demo() -> None:
    # Test 1: the hospital example from the project brief.
    print("TEST 1: hospital example (A: 2 free, 1000 m | B: 12 free, 1500 m | C: 0 free, 500 m)\n")
    zones = [
        ParkingZone("A", "Parking A", 10, 1000, 0),
        ParkingZone("B", "Parking B", 20, 1500, 0),
        ParkingZone("C", "Parking C", 10, 500, 0),
    ]
    statuses = [ZoneStatus.build(zones[0], 8), ZoneStatus.build(zones[1], 8), ZoneStatus.build(zones[2], 10)]
    predictions = [_prediction(z.zone_id, 50.0, "Medium") for z in zones]
    _print_result(recommend_parking(zones, statuses, predictions, (0, 0)))

    # Test 2: every zone is full.
    print("TEST 2: all zones full\n")
    full = [ZoneStatus.build(z, z.capacity) for z in zones]
    _print_result(recommend_parking(zones, full, predictions, (0, 0)))

    # Test 3: the real TRAFORRA zones with sample occupancy and live predictions.
    print("TEST 3: TRAFORRA demo city, destination = hospital\n")
    from .demand_predictor import DemandPredictor
    from .occupancy_monitor import load_zones

    city_zones = load_zones()
    sample = {"PA_A": 5, "PA_B": 3, "PA_C": 2, "PA_D": 12, "PA_E": 1}
    city_status = [ZoneStatus.build(z, sample[z.zone_id]) for z in city_zones]
    predictor = DemandPredictor.from_files()
    city_pred = predictor.predict(city_status, datetime(2026, 11, 24, 9, 30))
    _print_result(recommend_parking(city_zones, city_status, city_pred, "hospital"))


def main() -> None:
    parser = argparse.ArgumentParser(description="ParkSense recommendation engine")
    parser.add_argument("--demo", action="store_true", help="run the demo scenarios")
    args = parser.parse_args()
    config.setup_logging(logging.WARNING)
    if args.demo:
        run_demo()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()