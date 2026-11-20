"""ParkSense agent: the single public interface for the TRAFORRA orchestrator.

Public functions (all return JSON-serializable dicts):
  get_parking_status()
  calculate_occupancy(zone_id=None)
  predict_parking_demand(method="auto")
  recommend_parking(destination="hospital", top_n=3)
  get_parking_analytics(destination="hospital")

ParkSense does not depend on TrafficMind or GreenCorridor.

Run the demo from the project root:
    python -m backend.agents.park_sense.agent --demo
    python -m backend.agents.park_sense.agent --sumo
"""

import argparse
import json
import logging
from datetime import datetime
from typing import Dict, Optional

from . import config
from . import recommendation_engine as engine
from .demand_predictor import DemandPredictor
from .occupancy_monitor import OccupancyMonitor, load_zones
from .parking_analytics import calculate_analytics
from .schemas import InvalidParkingDataError

logger = logging.getLogger(__name__)


def _stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


class ParkSenseAgent:
    """Owns the monitor and predictor and exposes the public functions."""

    def __init__(self, zones_path=None, predictor: Optional[DemandPredictor] = None):
        self.zones = load_zones(zones_path)
        self.monitor = OccupancyMonitor(self.zones)
        self.predictor = predictor or DemandPredictor.from_files()

    # ----- feeding data -----
    def update_occupancy(self, counts: Dict[str, int], timestamp: float = 0.0) -> None:
        """Set how many cars are parked in each zone, e.g. {"PA_A": 5, "PA_B": 3}.

        Zones that are not mentioned keep their current value.
        Raises InvalidParkingDataError for unknown zones or impossible counts.
        """
        if not isinstance(counts, dict):
            raise InvalidParkingDataError("counts must be a dict of {zone_id: occupied}")
        for zone_id, n in counts.items():
            if zone_id not in self.monitor.zones:
                raise InvalidParkingDataError(f"Unknown zone_id: {zone_id!r}")
            cap = self.monitor.zones[zone_id].capacity
            if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > cap:
                raise InvalidParkingDataError(
                    f"Zone {zone_id}: occupied must be an integer from 0 to {cap}, got {n!r}"
                )
        for zone_id, n in counts.items():
            self.monitor.sync_zone(zone_id, [f"{zone_id}_veh{i}" for i in range(n)], timestamp)

    def update_from_sumo(self, timestamp: float = 0.0) -> None:
        """Read live occupancy from a running SUMO simulation (TraCI must be connected)."""
        self.monitor.update_from_sumo(timestamp)

    # ----- public functions -----
    def get_parking_status(self, now: Optional[datetime] = None) -> dict:
        """Live status of every zone.

        Output: {"timestamp", "zones": [{zone_id, name, capacity, occupied,
                 available, occupancy_pct, status}]}
        """
        return {
            "timestamp": _stamp(now),
            "zones": [s.to_dict() for s in self.monitor.get_all_status()],
        }

    def calculate_occupancy(self, zone_id: Optional[str] = None) -> dict:
        """Occupancy of one zone, or of the whole city when zone_id is None.

        Input:  zone_id such as "PA_A" (optional)
        Output: one zone dict, or {"total_capacity", "total_occupied",
                "total_available", "overall_occupancy_pct"}
        """
        if zone_id is not None:
            return self.monitor.calculate_occupancy(zone_id)
        statuses = self.monitor.get_all_status()
        cap = sum(s.capacity for s in statuses)
        occ = sum(s.occupied for s in statuses)
        return {
            "total_capacity": cap,
            "total_occupied": occ,
            "total_available": cap - occ,
            "overall_occupancy_pct": round(occ / cap * 100, 1),
        }

    def predict_parking_demand(self, now: Optional[datetime] = None, method: str = "auto") -> dict:
        """Predict demand 30 minutes ahead.

        Input:  now (datetime, optional), method "auto" | "rule_based" | "machine_learning"
        Output: {"timestamp", "horizon_minutes", "predictions": [{zone_id,
                 current_occupancy_pct, predicted_occupancy_pct, demand_level,
                 method, horizon_minutes}]}
        """
        statuses = self.monitor.get_all_status()
        preds = self.predictor.predict(statuses, now, method=method)
        return {
            "timestamp": _stamp(now),
            "horizon_minutes": self.predictor.horizon,
            "predictions": [p.to_dict() for p in preds],
        }

    def recommend_parking(self, destination=None, top_n: int = 3, now: Optional[datetime] = None) -> dict:
        """Rank parking zones for a destination.

        Input:  destination "hospital" | {"x":..,"y":..} | (x, y); top_n
        Output: {"destination", "recommendations", "best", "excluded", "message"}
        """
        statuses = self.monitor.get_all_status()
        preds = self.predictor.predict(statuses, now)
        return engine.recommend_parking(
            list(self.zones), statuses, preds,
            destination if destination is not None else config.DEFAULT_DESTINATION,
            top_n,
        )

    def get_parking_analytics(self, destination=None, now: Optional[datetime] = None) -> dict:
        """Totals, demand, most occupied zone and recommended zone in one dict."""
        statuses = self.monitor.get_all_status()
        preds = self.predictor.predict(statuses, now)
        rec = engine.recommend_parking(
            list(self.zones), statuses, preds,
            destination if destination is not None else config.DEFAULT_DESTINATION,
        )
        result = calculate_analytics(statuses, preds, rec)
        result["timestamp"] = _stamp(now)
        result["destination"] = rec["destination"]
        return result


# ---------- module-level functions (what the orchestrator calls) ----------
_default_agent: Optional[ParkSenseAgent] = None


def get_agent() -> ParkSenseAgent:
    """Return the shared default agent, creating it on first use."""
    global _default_agent
    if _default_agent is None:
        config.setup_logging()
        _default_agent = ParkSenseAgent()
    return _default_agent


def get_parking_status() -> dict:
    return get_agent().get_parking_status()


def calculate_occupancy(zone_id: Optional[str] = None) -> dict:
    return get_agent().calculate_occupancy(zone_id)


def predict_parking_demand(method: str = "auto") -> dict:
    return get_agent().predict_parking_demand(method=method)


def recommend_parking(destination=None, top_n: int = 3) -> dict:
    return get_agent().recommend_parking(destination, top_n)


def get_parking_analytics(destination=None) -> dict:
    return get_agent().get_parking_analytics(destination)


# ---------- demos ----------
def _show(title: str, data: dict) -> None:
    json.dumps(data)  # raises TypeError if anything is not JSON-serializable
    print(f"\n=== {title} ===")
    print(json.dumps(data, indent=2))


def run_demo() -> None:
    agent = ParkSenseAgent()
    agent.update_occupancy({"PA_A": 5, "PA_B": 3, "PA_C": 2, "PA_D": 12, "PA_E": 1})
    now = datetime(2026, 11, 24, 9, 30)

    _show("calculate_occupancy('PA_A')", agent.calculate_occupancy("PA_A"))
    _show("calculate_occupancy() for the whole city", agent.calculate_occupancy())

    rec = agent.recommend_parking("hospital", now=now)
    print("\n=== recommend_parking('hospital') ===")
    engine._print_result(rec)

    analytics = agent.get_parking_analytics("hospital", now=now)
    json.dumps(analytics)
    print("=== get_parking_analytics ===")
    for key in ("total_capacity", "total_occupied", "total_available",
                "overall_occupancy_pct", "city_demand_level", "demand_summary",
                "most_occupied_zone", "recommended_zone"):
        print(f"{key:<24}{analytics[key]}")

    print("\nAll outputs are JSON-serializable: True")

    # Invalid input must raise a clear error, not crash silently.
    try:
        agent.update_occupancy({"PA_A": 99})
    except InvalidParkingDataError as exc:
        print(f"Invalid data rejected correctly: {exc}")


def run_sumo_demo(stop_time: int = 300) -> None:
    import sumolib
    import traci

    agent = ParkSenseAgent()
    traci.start([sumolib.checkBinary("sumo"), "-c", str(config.SUMO_CONFIG_FILE),
                 "--no-step-log", "true", "--no-warnings", "true"])
    try:
        while traci.simulation.getTime() < stop_time:
            traci.simulationStep()
            agent.update_from_sumo(traci.simulation.getTime())
    finally:
        traci.close()

    print(f"Live SUMO data at t = {stop_time} s")
    analytics = agent.get_parking_analytics("hospital")
    for z in analytics["zones"]:
        print(f"{z['zone_id']}  {z['occupied']}/{z['capacity']}  {z['occupancy_pct']}%  demand: {z['demand_level']}")
    print(f"Recommended: {analytics['recommended_zone']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ParkSense agent demo")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true", help="run with sample occupancy")
    group.add_argument("--sumo", action="store_true", help="run with live SUMO data")
    args = parser.parse_args()
    config.setup_logging(logging.WARNING)
    run_sumo_demo() if args.sumo else run_demo()


if __name__ == "__main__":
    main()