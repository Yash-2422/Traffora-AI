"""Tests for SimulationInterface: connection handling and basic stepping."""

import json
import shutil

import pytest

from backend.agents.traffic_mind import simulation_interface as si
from backend.agents.traffic_mind.config import SimulationConfig
from backend.agents.traffic_mind.exceptions import (
    ScenarioNotFoundError,
    SimulationConnectionError,
    SimulationNotRunningError,
    SumoNotFoundError,
)

SCENARIO = SimulationConfig(scenario="mixed")
SUMO_READY = si.find_sumo_binary() is not None and SCENARIO.resolved_sumocfg().is_file()
needs_sumo = pytest.mark.skipif(
    not SUMO_READY,
    reason="SUMO or the built scenario is missing "
           "(run: python simulation/traffic_mind/build_scenario.py)",
)


# ---------------------------- no SUMO required ----------------------------- #
def test_missing_scenario_raises(tmp_path):
    config = SimulationConfig(sumocfg=tmp_path / "does_not_exist.sumocfg")
    with pytest.raises(ScenarioNotFoundError) as err:
        si.SimulationInterface(config).start()
    assert err.value.to_dict()["code"] == "SCENARIO_NOT_FOUND"


def test_missing_sumo_binary_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "x.sumocfg"
    cfg.write_text("<configuration/>")
    monkeypatch.delenv("SUMO_HOME", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(si, "_pip_sumo_home", lambda: None)

    with pytest.raises(SumoNotFoundError) as err:
        si.SimulationInterface(SimulationConfig(sumocfg=cfg)).start()
    assert isinstance(err.value, SimulationConnectionError)
    assert err.value.to_dict()["status"] == "error"


def test_step_before_start_raises():
    sim = si.SimulationInterface(SCENARIO)
    assert not sim.is_running
    with pytest.raises(SimulationNotRunningError):
        sim.step()
    with pytest.raises(SimulationNotRunningError):
        sim.vehicle_count()


def test_close_is_idempotent():
    sim = si.SimulationInterface(SCENARIO)
    sim.close()
    sim.close()
    assert not sim.is_running


def test_error_objects_are_json_serializable():
    payload = SimulationConnectionError("boom").to_dict()
    assert json.loads(json.dumps(payload)) == {
        "status": "error", "code": "SIMULATION_CONNECTION", "message": "boom",
    }


# ------------------------------ needs SUMO --------------------------------- #
@needs_sumo
def test_start_step_close():
    with si.SimulationInterface(SCENARIO) as sim:
        assert sim.is_running
        assert sim.step(30) == 30.0
        assert sim.vehicle_count() >= 0
        assert sim.halting_vehicle_count() >= 0
    assert not sim.is_running


@needs_sumo
def test_network_summary_has_nine_signals_and_is_json_serializable():
    with si.SimulationInterface(SCENARIO) as sim:
        summary = sim.network_summary()
    assert summary["traffic_light_count"] == 9
    assert all(len(tl["phases"]) >= 2 for tl in summary["traffic_lights"])
    json.dumps(summary)  # must not raise


@needs_sumo
def test_same_seed_is_reproducible():
    counts = []
    for _ in range(2):
        with si.SimulationInterface(SimulationConfig(scenario="mixed", seed=5)) as sim:
            sim.step(300)
            counts.append(sim.vehicle_count())
    assert counts[0] == counts[1]


@needs_sumo
def test_step_after_end_raises():
    config = SimulationConfig(scenario="mixed", end_time=20)
    with si.SimulationInterface(config) as sim:
        sim.step(50)  # stops at end time
        assert sim.has_ended
        with pytest.raises(SimulationNotRunningError):
            sim.step()


@needs_sumo
def test_restart_after_close_works():
    sim = si.SimulationInterface(SCENARIO)
    for _ in range(2):
        sim.start()
        sim.step(5)
        sim.close()