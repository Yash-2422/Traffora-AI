"""ParkSense data structures. Every to_dict() output is JSON-serializable."""

from dataclasses import dataclass, asdict
from typing import Optional


class InvalidParkingDataError(ValueError):
    """Raised when parking data is missing or inconsistent."""


@dataclass
class ParkingZone:
    """Static description of a parking zone."""
    zone_id: str
    name: str
    capacity: int
    x: float
    y: float
    lane: str = ""

    def __post_init__(self):
        if not self.zone_id or not isinstance(self.zone_id, str):
            raise InvalidParkingDataError("zone_id must be a non-empty string")
        if not isinstance(self.capacity, int) or isinstance(self.capacity, bool) or self.capacity <= 0:
            raise InvalidParkingDataError(
                f"Zone {self.zone_id}: capacity must be a positive integer, got {self.capacity!r}"
            )

    @classmethod
    def from_dict(cls, data: dict) -> "ParkingZone":
        try:
            return cls(
                zone_id=data["zone_id"],
                name=data["name"],
                capacity=data["capacity"],
                x=float(data["x"]),
                y=float(data["y"]),
                lane=data.get("lane", ""),
            )
        except KeyError as exc:
            raise InvalidParkingDataError(f"Zone data is missing field: {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise InvalidParkingDataError(f"Invalid zone data: {exc}") from exc

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ZoneStatus:
    """Live occupancy of one zone."""
    zone_id: str
    name: str
    capacity: int
    occupied: int
    available: int
    occupancy_pct: float
    status: str  # "OPEN" or "FULL"

    @classmethod
    def build(cls, zone: ParkingZone, occupied: int) -> "ZoneStatus":
        if not isinstance(occupied, int) or isinstance(occupied, bool):
            raise InvalidParkingDataError(
                f"Zone {zone.zone_id}: occupied must be an integer, got {occupied!r}"
            )
        if occupied < 0 or occupied > zone.capacity:
            raise InvalidParkingDataError(
                f"Zone {zone.zone_id}: occupied={occupied} is outside 0..{zone.capacity}"
            )
        available = zone.capacity - occupied
        return cls(
            zone_id=zone.zone_id,
            name=zone.name,
            capacity=zone.capacity,
            occupied=occupied,
            available=available,
            occupancy_pct=round(occupied / zone.capacity * 100, 1),
            status="FULL" if available == 0 else "OPEN",
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DemandPrediction:
    """Predicted demand for one zone."""
    zone_id: str
    current_occupancy_pct: float
    predicted_occupancy_pct: float
    demand_level: str          # "Low", "Medium" or "High"
    method: str                # "rule_based" or "machine_learning"
    horizon_minutes: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParkingRecommendation:
    """One ranked parking suggestion."""
    rank: int
    zone_id: str
    name: str
    available_spaces: int
    distance_m: float
    eta_seconds: float
    demand_level: str
    score: float
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)