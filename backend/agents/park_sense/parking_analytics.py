"""ParkSense parking analytics.

Combines live status, demand predictions and the recommendation into one summary.
"""

import logging
from typing import List, Optional

from .demand_predictor import classify_demand
from .schemas import DemandPrediction, InvalidParkingDataError, ZoneStatus

logger = logging.getLogger(__name__)


def calculate_analytics(
    statuses: List[ZoneStatus],
    predictions: Optional[List[DemandPrediction]] = None,
    recommendation: Optional[dict] = None,
) -> dict:
    """Build the analytics summary.

    Input:
      statuses        - ZoneStatus list (live occupancy)
      predictions     - DemandPrediction list (optional)
      recommendation  - result dict from recommend_parking (optional)
    Output (JSON-serializable dict):
      {
        "total_capacity", "total_occupied", "total_available",
        "overall_occupancy_pct",
        "city_demand_level",            # Low / Medium / High
        "demand_summary": {"Low": n, "Medium": n, "High": n},
        "most_occupied_zone": {...} or None,
        "recommended_zone": {...} or None,
        "zones": [per-zone status + demand]
      }
    """
    if not statuses:
        raise InvalidParkingDataError("No zone statuses were given to calculate_analytics")

    total_capacity = sum(s.capacity for s in statuses)
    total_occupied = sum(s.occupied for s in statuses)
    total_available = sum(s.available for s in statuses)
    overall_pct = round(total_occupied / total_capacity * 100, 1) if total_capacity else 0.0

    pred_map = {p.zone_id: p for p in (predictions or [])}

    zones = []
    summary = {"Low": 0, "Medium": 0, "High": 0}
    for s in statuses:
        entry = s.to_dict()
        pred = pred_map.get(s.zone_id)
        if pred:
            entry["predicted_occupancy_pct"] = pred.predicted_occupancy_pct
            entry["demand_level"] = pred.demand_level
            summary[pred.demand_level] += 1
        else:
            entry["predicted_occupancy_pct"] = None
            entry["demand_level"] = "Unknown"
        zones.append(entry)

    if pred_map:
        avg_predicted = sum(p.predicted_occupancy_pct for p in pred_map.values()) / len(pred_map)
        city_demand = classify_demand(avg_predicted)
    else:
        city_demand = classify_demand(overall_pct)

    most_occupied = max(statuses, key=lambda s: (s.occupancy_pct, s.occupied))
    best = recommendation.get("best") if recommendation else None

    logger.info("Analytics: %d/%d spaces occupied (%.1f%%)", total_occupied, total_capacity, overall_pct)
    return {
        "total_capacity": total_capacity,
        "total_occupied": total_occupied,
        "total_available": total_available,
        "overall_occupancy_pct": overall_pct,
        "city_demand_level": city_demand,
        "demand_summary": summary,
        "most_occupied_zone": {
            "zone_id": most_occupied.zone_id,
            "name": most_occupied.name,
            "occupancy_pct": most_occupied.occupancy_pct,
        },
        "recommended_zone": (
            {"zone_id": best["zone_id"], "name": best["name"], "score": best["score"]} if best else None
        ),
        "zones": zones,
    }