"""ParkSense occupancy monitor.

Tracks which vehicles are parked in each zone, detects entries and exits,
and calculates available spaces and occupancy percentage.

Run demos from the project root:
    python -m backend.agents.park_sense.occupancy_monitor --mode simulated
    python -m backend.agents.park_sense.occupancy_monitor --mode sumo
"""

import argparse
import json
import logging
from typing import Dict, List, Optional, Set

from . import config
from .schemas import InvalidParkingDataError, ParkingZone, ZoneStatus

logger = logging.getLogger(__name__)

MAX_EVENTS_KEPT = 1000  # keep memory use bounded


def load_zones(path=None) -> List[ParkingZone]:
    """Load parking zones from the zones JSON file.

    Input:  path to zones.json (defaults to config.ZONES_FILE)
    Output: list of ParkingZone objects
    Raises: InvalidParkingDataError if the file is missing or invalid
    """
    path = path or config.ZONES_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise InvalidParkingDataError(f"Zones file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InvalidParkingDataError(f"Zones file is not valid JSON: {exc}") from exc

    raw_zones = data.get("zones") if isinstance(data, dict) else None
    if not raw_zones:
        raise InvalidParkingDataError("Zones file must contain a non-empty 'zones' list")

    zones = [ParkingZone.from_dict(z) for z in raw_zones]
    ids = [z.zone_id for z in zones]
    if len(ids) != len(set(ids)):
        raise InvalidParkingDataError("Duplicate zone_id found in zones file")

    logger.info("Loaded %d parking zones", len(zones))
    return zones


class OccupancyMonitor:
    """Keeps track of parked vehicles per zone."""

    def __init__(self, zones: List[ParkingZone]):
        if not zones:
            raise InvalidParkingDataError("OccupancyMonitor needs at least one zone")
        self.zones: Dict[str, ParkingZone] = {z.zone_id: z for z in zones}
        self._parked: Dict[str, Set[str]] = {zid: set() for zid in self.zones}
        self.events: List[dict] = []

    # ---------- helpers ----------
    def _get_zone(self, zone_id: str) -> ParkingZone:
        if zone_id not in self.zones:
            raise InvalidParkingDataError(f"Unknown zone_id: {zone_id!r}")
        return self.zones[zone_id]

    def _record_event(self, kind: str, zone_id: str, vehicle_id: str, timestamp: float) -> None:
        self.events.append(
            {"time": timestamp, "event": kind, "zone_id": zone_id, "vehicle_id": vehicle_id}
        )
        if len(self.events) > MAX_EVENTS_KEPT:
            self.events = self.events[-MAX_EVENTS_KEPT:]

    # ---------- entry and exit ----------
    def vehicle_enter(self, zone_id: str, vehicle_id: str, timestamp: float = 0.0) -> bool:
        """A vehicle enters a zone.

        Returns True if accepted, False if the zone is full or the vehicle is already parked there.
        """
        zone = self._get_zone(zone_id)
        if not vehicle_id:
            raise InvalidParkingDataError("vehicle_id must be a non-empty string")
        parked = self._parked[zone_id]
        if vehicle_id in parked:
            logger.warning("Vehicle %s is already parked in %s", vehicle_id, zone_id)
            return False
        if len(parked) >= zone.capacity:
            logger.warning("Zone %s is full, vehicle %s rejected", zone_id, vehicle_id)
            return False
        parked.add(vehicle_id)
        self._record_event("ENTRY", zone_id, vehicle_id, timestamp)
        logger.info("ENTRY  %s -> %s (%d/%d)", vehicle_id, zone_id, len(parked), zone.capacity)
        return True

    def vehicle_exit(self, zone_id: str, vehicle_id: str, timestamp: float = 0.0) -> bool:
        """A vehicle leaves a zone. Returns False if it was not parked there."""
        self._get_zone(zone_id)
        parked = self._parked[zone_id]
        if vehicle_id not in parked:
            logger.warning("Vehicle %s is not parked in %s", vehicle_id, zone_id)
            return False
        parked.remove(vehicle_id)
        self._record_event("EXIT", zone_id, vehicle_id, timestamp)
        logger.info("EXIT   %s <- %s (%d/%d)", vehicle_id, zone_id, len(parked), self.zones[zone_id].capacity)
        return True

    def sync_zone(self, zone_id: str, vehicle_ids: List[str], timestamp: float = 0.0) -> None:
        """Replace a zone's parked vehicles with a fresh list and detect entries/exits.

        This is what SUMO mode uses every few steps.
        """
        zone = self._get_zone(zone_id)
        current = set(vehicle_ids)
        if len(current) > zone.capacity:
            logger.warning(
                "Zone %s reports %d vehicles but capacity is %d; extra vehicles ignored",
                zone_id, len(current), zone.capacity,
            )
            current = set(sorted(current)[: zone.capacity])

        previous = self._parked[zone_id]
        for vid in sorted(current - previous):
            self._record_event("ENTRY", zone_id, vid, timestamp)
            logger.info("ENTRY  %s -> %s", vid, zone_id)
        for vid in sorted(previous - current):
            self._record_event("EXIT", zone_id, vid, timestamp)
            logger.info("EXIT   %s <- %s", vid, zone_id)
        self._parked[zone_id] = current

    # ---------- SUMO / TraCI ----------
    def update_from_sumo(self, timestamp: float = 0.0) -> None:
        """Read every parking area from a running SUMO simulation through TraCI."""
        import traci  # imported here so simulated mode works without SUMO

        for zone_id in self.zones:
            try:
                vehicle_ids = list(traci.parkingarea.getVehicleIDs(zone_id))
            except traci.TraCIException as exc:
                logger.error("Could not read parking area %s: %s", zone_id, exc)
                continue
            self.sync_zone(zone_id, vehicle_ids, timestamp)

    # ---------- results ----------
    def get_zone_status(self, zone_id: str) -> ZoneStatus:
        """Current status of one zone (capacity, occupied, available, occupancy %, status)."""
        zone = self._get_zone(zone_id)
        return ZoneStatus.build(zone, len(self._parked[zone_id]))

    def get_all_status(self) -> List[ZoneStatus]:
        """Current status of every zone."""
        return [self.get_zone_status(zid) for zid in self.zones]

    def calculate_occupancy(self, zone_id: str) -> dict:
        """Occupancy numbers of one zone as a JSON-friendly dict."""
        return self.get_zone_status(zone_id).to_dict()

    def get_recent_events(self, limit: int = 20) -> List[dict]:
        """Most recent entry/exit events."""
        return self.events[-limit:]


# ---------- demos ----------
def _print_table(monitor: OccupancyMonitor, title: str) -> None:
    print(f"\n{title}")
    print(f"{'Zone':<6}{'Name':<26}{'Cap':>4}{'Occ':>5}{'Free':>6}{'Occ %':>8}  Status")
    for s in monitor.get_all_status():
        print(f"{s.zone_id:<6}{s.name:<26}{s.capacity:>4}{s.occupied:>5}{s.available:>6}{s.occupancy_pct:>8}  {s.status}")


def run_simulated_demo() -> None:
    """Demo without SUMO: fill a zone, try to overfill it, then free a space."""
    monitor = OccupancyMonitor(load_zones())
    for i in range(1, 7):
        monitor.vehicle_enter("PA_A", f"car{i}", timestamp=i)
    _print_table(monitor, "After 6 cars entered PA_A (capacity 6)")

    accepted = monitor.vehicle_enter("PA_A", "car7", timestamp=10)
    print(f"\nCar 7 accepted? {accepted}  (expected False, zone is full)")

    monitor.vehicle_exit("PA_A", "car3", timestamp=20)
    _print_table(monitor, "After car3 left PA_A")
    print("\nRecent events:")
    print(json.dumps(monitor.get_recent_events(3), indent=2))


def run_sumo_demo(max_steps: int = 900, print_every: int = 60) -> None:
    """Demo with SUMO: run the scenario headless and print occupancy over time."""
    import sumolib
    import traci

    monitor = OccupancyMonitor(load_zones())
    sumo_binary = sumolib.checkBinary("sumo")
    traci.start([sumo_binary, "-c", str(config.SUMO_CONFIG_FILE), "--no-step-log", "true", "--no-warnings", "true"])
    try:
        step = 0
        while step < max_steps and traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            step += 1
            now = traci.simulation.getTime()
            monitor.update_from_sumo(now)
            if step % print_every == 0:
                _print_table(monitor, f"Simulation time: {now:.0f} s")
    finally:
        traci.close()

    print(f"\nTotal entry/exit events recorded: {len(monitor.events)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ParkSense occupancy monitor demo")
    parser.add_argument("--mode", choices=["simulated", "sumo"], default="simulated")
    args = parser.parse_args()
    config.setup_logging(logging.WARNING)  # keep the demo output tidy
    if args.mode == "sumo":
        run_sumo_demo()
    else:
        run_simulated_demo()


if __name__ == "__main__":
    main()