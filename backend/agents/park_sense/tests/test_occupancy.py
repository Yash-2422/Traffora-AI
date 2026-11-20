"""Tests: occupancy monitoring, vehicle entry/exit, available spaces, percentages, invalid data."""

import json

import pytest

from backend.agents.park_sense.occupancy_monitor import OccupancyMonitor, load_zones
from backend.agents.park_sense.schemas import InvalidParkingDataError, ParkingZone, ZoneStatus


@pytest.fixture
def monitor():
    zones = [ParkingZone("Z1", "Zone 1", 4, 0, 0), ParkingZone("Z2", "Zone 2", 10, 100, 0)]
    return OccupancyMonitor(zones)


# ---------- occupancy numbers ----------
def test_new_zone_is_empty(monitor):
    s = monitor.get_zone_status("Z1")
    assert (s.capacity, s.occupied, s.available) == (4, 0, 4)
    assert s.occupancy_pct == 0.0
    assert s.status == "OPEN"


def test_available_spaces_calculation(monitor):
    monitor.vehicle_enter("Z1", "a")
    monitor.vehicle_enter("Z1", "b")
    s = monitor.get_zone_status("Z1")
    assert s.occupied == 2
    assert s.available == 2


def test_occupancy_percentage(monitor):
    for i in range(3):
        monitor.vehicle_enter("Z1", f"v{i}")
        monitor.vehicle_enter("Z2", f"w{i}")
    assert monitor.get_zone_status("Z1").occupancy_pct == 75.0
    assert monitor.get_zone_status("Z2").occupancy_pct == 30.0


def test_calculate_occupancy_returns_json_friendly_dict(monitor):
    monitor.vehicle_enter("Z1", "a")
    result = monitor.calculate_occupancy("Z1")
    assert result["occupancy_pct"] == 25.0
    json.dumps(result)  # must not raise


def test_get_all_status_returns_every_zone(monitor):
    assert [s.zone_id for s in monitor.get_all_status()] == ["Z1", "Z2"]


# ---------- entry and exit ----------
def test_vehicle_entry_is_accepted(monitor):
    assert monitor.vehicle_enter("Z1", "a") is True
    assert monitor.get_zone_status("Z1").occupied == 1


def test_vehicle_exit_frees_a_space(monitor):
    monitor.vehicle_enter("Z1", "a")
    assert monitor.vehicle_exit("Z1", "a") is True
    assert monitor.get_zone_status("Z1").occupied == 0


def test_full_zone_rejects_new_vehicle(monitor):
    for i in range(4):
        assert monitor.vehicle_enter("Z1", f"v{i}") is True
    assert monitor.vehicle_enter("Z1", "extra") is False
    s = monitor.get_zone_status("Z1")
    assert s.status == "FULL"
    assert s.available == 0
    assert s.occupancy_pct == 100.0


def test_duplicate_entry_rejected(monitor):
    monitor.vehicle_enter("Z1", "a")
    assert monitor.vehicle_enter("Z1", "a") is False
    assert monitor.get_zone_status("Z1").occupied == 1


def test_exit_of_unknown_vehicle_returns_false(monitor):
    assert monitor.vehicle_exit("Z1", "ghost") is False


def test_events_are_recorded(monitor):
    monitor.vehicle_enter("Z1", "a", timestamp=5)
    monitor.vehicle_exit("Z1", "a", timestamp=9)
    events = monitor.get_recent_events(10)
    assert [(e["event"], e["vehicle_id"], e["time"]) for e in events] == [
        ("ENTRY", "a", 5), ("EXIT", "a", 9)
    ]


def test_sync_zone_detects_entries_and_exits(monitor):
    monitor.sync_zone("Z1", ["a", "b"], timestamp=1)
    monitor.sync_zone("Z1", ["b", "c"], timestamp=2)
    events = [(e["event"], e["vehicle_id"]) for e in monitor.get_recent_events(10)]
    assert events == [("ENTRY", "a"), ("ENTRY", "b"), ("ENTRY", "c"), ("EXIT", "a")]
    assert monitor.get_zone_status("Z1").occupied == 2


def test_sync_zone_never_exceeds_capacity(monitor):
    monitor.sync_zone("Z1", [f"v{i}" for i in range(9)])
    assert monitor.get_zone_status("Z1").occupied == 4


# ---------- invalid data ----------
def test_unknown_zone_raises(monitor):
    with pytest.raises(InvalidParkingDataError):
        monitor.vehicle_enter("NOPE", "a")
    with pytest.raises(InvalidParkingDataError):
        monitor.get_zone_status("NOPE")


def test_empty_vehicle_id_raises(monitor):
    with pytest.raises(InvalidParkingDataError):
        monitor.vehicle_enter("Z1", "")


@pytest.mark.parametrize("bad_capacity", [0, -5, 2.5, "10", None, True])
def test_invalid_capacity_raises(bad_capacity):
    with pytest.raises(InvalidParkingDataError):
        ParkingZone("Z", "Bad zone", bad_capacity, 0, 0)


def test_empty_zone_id_raises():
    with pytest.raises(InvalidParkingDataError):
        ParkingZone("", "No id", 5, 0, 0)


def test_zone_from_dict_missing_field_raises():
    with pytest.raises(InvalidParkingDataError):
        ParkingZone.from_dict({"zone_id": "A"})


def test_zone_from_dict_bad_coordinates_raises():
    with pytest.raises(InvalidParkingDataError):
        ParkingZone.from_dict({"zone_id": "A", "name": "A", "capacity": 5, "x": "abc", "y": 0})


@pytest.mark.parametrize("occupied", [-1, 5, 2.0, "2", None])
def test_zone_status_rejects_impossible_occupied(occupied):
    zone = ParkingZone("Z", "Zone", 4, 0, 0)
    with pytest.raises(InvalidParkingDataError):
        ZoneStatus.build(zone, occupied)


def test_monitor_needs_at_least_one_zone():
    with pytest.raises(InvalidParkingDataError):
        OccupancyMonitor([])


# ---------- loading zones from a file ----------
def test_load_real_zones_file():
    zones = load_zones()
    assert len(zones) == 5
    assert sum(z.capacity for z in zones) == 51


def test_load_zones_missing_file(tmp_path):
    with pytest.raises(InvalidParkingDataError):
        load_zones(tmp_path / "missing.json")


def test_load_zones_bad_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(InvalidParkingDataError):
        load_zones(bad)


def test_load_zones_empty_list(tmp_path):
    f = tmp_path / "empty.json"
    f.write_text(json.dumps({"zones": []}), encoding="utf-8")
    with pytest.raises(InvalidParkingDataError):
        load_zones(f)


def test_load_zones_duplicate_ids(tmp_path):
    zone = {"zone_id": "A", "name": "A", "capacity": 5, "x": 0, "y": 0}
    f = tmp_path / "dup.json"
    f.write_text(json.dumps({"zones": [zone, zone]}), encoding="utf-8")
    with pytest.raises(InvalidParkingDataError):
        load_zones(f)