"""Tests for the single-SUMO-session coordinator, with a fake TraCI backend.

The coordinator is the only module that imports TraCI. These tests replace the
underlying ``SimulationInterface`` with a fake so the *contract* can be checked
without launching SUMO: one session, reads shaped for the validator, and
writes restricted to ``setPhaseDuration`` on validated actions only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.orchestrator.config import OrchestratorConfig
from backend.orchestrator.exceptions import (
    SafetyViolationError,
    SimulationNotRunningError,
    SimulationStartError,
)
from backend.orchestrator.schemas import ValidationResult, ValidationStatus
from backend.orchestrator.simulation_coordinator import SimulationCoordinator

from .conftest import make_signal_action


class FakeTraCI:
    """Minimal labelled TraCI connection."""

    def __init__(self, tls_ids=("A1", "B1"), program_id="orchestrator") -> None:
        self.writes = []
        self.program_switches = []
        self._tls_ids = list(tls_ids)
        self._program_id = program_id
        self._phase = {tls: 0 for tls in tls_ids}
        self._state = {tls: "GGGrrr" for tls in tls_ids}
        self._next_switch = {tls: 20.0 for tls in tls_ids}
        self._time = 0.0
        self._parked = {tls: 0 for tls in tls_ids}

        outer = self

        class _TrafficLight:
            def getIDList(self):  # noqa: N802
                return list(outer._tls_ids)

            def getPhase(self, tls):  # noqa: N802
                return outer._phase[tls]

            def getRedYellowGreenState(self, tls):  # noqa: N802
                return outer._state[tls]

            def getNextSwitch(self, tls):  # noqa: N802
                return outer._next_switch[tls]

            def getProgram(self, tls):  # noqa: N802
                return outer._program_id

            def getAllProgramLogics(self, tls):  # noqa: N802
                phases = [
                    SimpleNamespace(minDur=15.0, maxDur=60.0),
                    SimpleNamespace(minDur=3.0, maxDur=3.0),
                    SimpleNamespace(minDur=2.0, maxDur=2.0),
                ]
                return [SimpleNamespace(programID=outer._program_id, phases=phases)]

            def setProgram(self, tls, program_id):  # noqa: N802
                outer.program_switches.append((tls, program_id))

            def setPhaseDuration(self, tls, duration):  # noqa: N802
                outer.writes.append((tls, float(duration)))

            def setPhase(self, *a, **k):  # pragma: no cover
                raise AssertionError("setPhase must never be called")

            def setRedYellowGreenState(self, *a, **k):  # pragma: no cover
                raise AssertionError("setRedYellowGreenState must never be called")

        class _Simulation:
            def getTime(self):  # noqa: N802
                return outer._time

            def getDepartedIDList(self):  # noqa: N802
                return []

            def getArrivedIDList(self):  # noqa: N802
                return []

        class _ParkingArea:
            def getIDList(self):  # noqa: N802
                return ["PA_A"]

            def getVehicleCount(self, area):  # noqa: N802
                return outer._parked.get(area, 0)

        self.trafficlight = _TrafficLight()
        self.simulation = _Simulation()
        self.parkingarea = _ParkingArea()


class FakeSimulationInterface:
    def __init__(self, connection=None, fail_start: bool = False) -> None:
        self._conn = connection or FakeTraCI()
        self.is_running = False
        self.has_ended = False
        self.current_time = 0.0
        self.fail_start = fail_start
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("SUMO binary not found")
        self.is_running = True

    def step(self, n: int = 1) -> float:
        self.current_time += n
        self._conn._time = self.current_time
        return self.current_time

    def close(self) -> None:
        self.close_calls += 1
        self.is_running = False


@pytest.fixture
def coordinator() -> SimulationCoordinator:
    coord = SimulationCoordinator(OrchestratorConfig())
    coord._sim = FakeSimulationInterface()
    return coord


def _valid(action) -> dict:
    return {action.action_id: ValidationResult(valid=True, status=ValidationStatus.VALID)}


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_start_opens_one_session_and_reads_traffic_lights(coordinator):
    coordinator.start()
    assert coordinator.is_running
    assert coordinator.known_tls_ids == {"A1", "B1"}
    assert coordinator._sim.start_calls == 1


def test_start_activates_the_authored_signal_program(coordinator):
    coordinator.start()
    assert coordinator._sim._conn.program_switches == [
        ("A1", "orchestrator"), ("B1", "orchestrator")]


def test_start_failure_is_wrapped(coordinator):
    coordinator._sim = FakeSimulationInterface(fail_start=True)
    with pytest.raises(SimulationStartError):
        coordinator.start()


def test_close_is_idempotent_and_forgets_lights(coordinator):
    coordinator.start()
    coordinator.close()
    coordinator.close()
    assert coordinator._sim.close_calls == 2
    assert coordinator.known_tls_ids == set()


def test_step_advances_time_and_tracks_phases(coordinator):
    coordinator.start()
    now = coordinator.step(3)
    assert now == 3.0
    assert coordinator.signal_state("A1")["elapsed_s"] == 0.0
    coordinator.step(5)
    assert coordinator.signal_state("A1")["elapsed_s"] == 5.0


def test_step_before_start_raises(coordinator):
    with pytest.raises(SimulationNotRunningError):
        coordinator.step()


def test_current_time_before_start_raises(coordinator):
    with pytest.raises(SimulationNotRunningError):
        _ = coordinator.current_time


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def test_signal_state_is_shaped_for_the_validator(coordinator):
    coordinator.start()
    coordinator.step(1)
    state = coordinator.signal_state("A1")

    assert state["tls_id"] == "A1"
    assert state["phase_kind"] == "GREEN"
    assert state["min_dur"] == 15.0
    assert state["max_dur"] == 60.0
    assert state["remaining_s"] == pytest.approx(19.0)


def test_signal_state_classifies_yellow_and_all_red(coordinator):
    coordinator.start()
    coordinator.step(1)
    coordinator._sim._conn._state["A1"] = "yyyrrr"
    assert coordinator.signal_state("A1")["phase_kind"] == "YELLOW"
    coordinator._sim._conn._state["A1"] = "rrrrrr"
    assert coordinator.signal_state("A1")["phase_kind"] == "RED"


def test_unknown_signal_returns_none(coordinator):
    coordinator.start()
    assert coordinator.signal_state("Z9") is None


def test_read_parking_occupancy_uses_sim_state(coordinator):
    coordinator.start()
    coordinator._sim._conn._parked["PA_A"] = 4
    assert coordinator.read_parking_occupancy() == {"PA_A": 4}


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def test_apply_writes_only_set_phase_duration(coordinator):
    coordinator.start()
    action = make_signal_action("A1", 25.0)

    records = coordinator.apply([action], _valid(action))

    assert coordinator._sim._conn.writes == [("A1", 25.0)]
    assert records[0]["action_id"] == action.action_id
    assert coordinator.applied_action_count == 1


def test_apply_refuses_an_unvalidated_action(coordinator):
    coordinator.start()
    action = make_signal_action("A1", 25.0)

    with pytest.raises(SafetyViolationError):
        coordinator.apply([action], {})

    assert coordinator._sim._conn.writes == []


def test_apply_refuses_the_whole_batch_if_one_action_is_rejected(coordinator):
    coordinator.start()
    good = make_signal_action("A1", 25.0, action_id="good")
    bad = make_signal_action("B1", 25.0, action_id="bad")
    validations = {
        "good": ValidationResult(valid=True, status=ValidationStatus.VALID),
        "bad": ValidationResult(valid=False, status=ValidationStatus.REJECTED),
    }

    with pytest.raises(SafetyViolationError):
        coordinator.apply([good, bad], validations)

    assert coordinator._sim._conn.writes == []


def test_apply_before_start_raises(coordinator):
    action = make_signal_action("A1", 25.0)
    with pytest.raises(SimulationNotRunningError):
        coordinator.apply([action], _valid(action))
