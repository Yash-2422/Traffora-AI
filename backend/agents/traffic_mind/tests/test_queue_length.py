"""Tests for the queue-length measurement in SimulationInterface.

These use a fake TraCI connection, so they run without SUMO. The positions in
``test_discharging_queue_from_real_run`` come from a real simulation run
(road A1B1 at t=1400 s) where a green phase had just started: the first cars
crept at 0.3-1.0 m/s and an earlier version measured 6 m instead of ~150 m.
"""

import pytest

from backend.agents.traffic_mind.config import SimulationConfig
from backend.agents.traffic_mind.schemas import EdgeInfo
from backend.agents.traffic_mind.simulation_interface import SimulationInterface

LENGTH = 235.6
EDGE = EdgeInfo(edge_id="R", length_m=LENGTH, lane_count=1, speed_limit_mps=13.89)


class _FakeVehicle:
    def __init__(self, cars):
        self._cars = cars  # id -> (lane position, speed)

    def getLanePosition(self, vid):
        return self._cars[vid][0]

    def getSpeed(self, vid):
        return self._cars[vid][1]


class _FakeLane:
    def __init__(self, cars):
        self._ids = list(cars)

    def getLastStepVehicleIDs(self, lane_id):
        return self._ids


class _FakeConn:
    def __init__(self, cars):
        self.vehicle = _FakeVehicle(cars)
        self.lane = _FakeLane(cars)


def measure(dist_speed_pairs, config=None):
    """Queue length for cars given as (distance to stop line, speed)."""
    cars = {f"v{i}": (LENGTH - dist, speed) for i, (dist, speed) in enumerate(dist_speed_pairs)}
    sim = SimulationInterface(config or SimulationConfig())
    sim._conn = _FakeConn(cars)
    return sim._queue_length_m(EDGE)


def test_no_vehicles_means_no_queue():
    assert measure([]) == 0.0


def test_free_flowing_traffic_has_no_queue():
    assert measure([(5, 12.0), (40, 13.0), (120, 13.5)]) == 0.0


def test_standing_queue_at_the_stop_line():
    # 5 stopped cars, 7.5 m apart; last car is 31 m from the line -> 31 + 5 m car length
    queue = measure([(1.0 + 7.5 * i, 0.0) for i in range(5)])
    assert queue == pytest.approx(31.0 + 5.0)


def test_discharging_queue_from_real_run():
    # road A1B1 at t=1400 s: first cars creeping (0.01, 0.34, 1.04, 0.72 m/s), then standing
    cars = [
        (1.01, 0.01), (8.76, 0.34), (16.75, 1.04), (25.31, 0.72), (33.54, 0.0),
        (41.04, 0.0), (48.54, 0.0), (56.04, 0.0), (63.54, 0.0), (71.04, 0.0),
        (78.54, 0.0), (86.04, 0.0), (93.54, 0.0), (101.05, 0.0), (108.55, 0.0),
        (116.06, 0.01), (123.59, 0.01), (131.14, 0.06), (138.74, 0.05), (146.38, 0.09),
    ]
    queue = measure(cars)
    assert queue == pytest.approx(146.38 + 5.0, abs=0.01)  # was 6.0 with the 0.1 m/s rule


def test_head_of_queue_has_pulled_away():
    # front cars already moving at 2-4 m/s; the standing queue starts 18.5 m from the line
    cars = [(8.0, 4.7), (18.5, 0.0), (26.0, 0.0), (33.5, 0.0), (41.0, 0.0)]
    assert measure(cars) == pytest.approx(41.0 + 5.0)


def test_slow_car_far_from_the_stop_line_is_not_a_queue():
    assert measure([(100.0, 0.0), (107.5, 0.0)]) == 0.0


def test_gap_in_the_line_of_cars_ends_the_queue():
    # 3 cars bunched at the line, next stopped car is 30 m behind them -> separate, not counted
    cars = [(1.0, 0.0), (8.5, 0.0), (16.0, 0.0), (47.0, 0.0), (54.5, 0.0)]
    assert measure(cars) == pytest.approx(16.0 + 5.0)


def test_queue_never_exceeds_road_length():
    cars = [(0.5 + 7.5 * i, 0.0) for i in range(40)]  # more cars than fit
    assert measure(cars) <= LENGTH


def test_speed_threshold_is_configurable():
    cars = [(1.0, 1.0), (8.5, 1.0)]
    assert measure(cars) > 0
    assert measure(cars, SimulationConfig(queue_speed_mps=0.5)) == 0.0