"""SUMO/TraCI connection handling (testing requirement #12)."""
import pytest

from backend.agents.green_corridor import connection
from backend.agents.green_corridor.connection import SumoConnection, SumoConnectionError
from .conftest import requires_sumo


def test_missing_config_gives_clear_error(tmp_path):
    sc = SumoConnection()
    with pytest.raises(SumoConnectionError) as e:
        sc.start(tmp_path / "nope.sumocfg")
    assert e.value.code == "CONFIG_NOT_FOUND"
    assert not sc.active


def test_traci_access_before_start_raises():
    with pytest.raises(SumoConnectionError) as e:
        SumoConnection().traci
    assert e.value.code == "NOT_CONNECTED"


def test_sumo_binary_missing(monkeypatch, tmp_path):
    cfg = tmp_path / "x.sumocfg"
    cfg.write_text("<configuration/>")
    monkeypatch.setattr(connection, "find_binary", lambda name: None)
    with pytest.raises(SumoConnectionError) as e:
        SumoConnection().start(cfg)
    assert e.value.code == "SUMO_NOT_FOUND"


def test_error_is_json_serializable():
    import json
    err = SumoConnectionError("X", "msg")
    assert json.loads(json.dumps(err.to_dict())) == {"code": "X", "message": "msg"}


@requires_sumo
def test_start_step_close(smoke_cfg):
    with SumoConnection() as sc:
        sc.start(smoke_cfg)
        assert sc.active
        t1 = sc.step()
        t2 = sc.step()
        assert t2 > t1
    assert not sc.active


@requires_sumo
def test_bad_scenario_reports_start_failed(tmp_path):
    bad = tmp_path / "bad.sumocfg"
    bad.write_text('<configuration><input><net-file value="missing.net.xml"/></input></configuration>')
    sc = SumoConnection()
    with pytest.raises(SumoConnectionError) as e:
        sc.start(bad)
    assert e.value.code == "START_FAILED"
    assert not sc.active


@requires_sumo
def test_double_start_rejected(smoke_cfg):
    with SumoConnection() as sc:
        sc.start(smoke_cfg)
        with pytest.raises(SumoConnectionError) as e:
            sc.start(smoke_cfg)
        assert e.value.code == "ALREADY_CONNECTED"


@requires_sumo
def test_connection_lost_is_detected_and_cleaned_up(smoke_cfg):
    sc = SumoConnection()
    sc.start(smoke_cfg)
    sc.step()
    sc.traci._process.kill()      # simulate SUMO crashing mid-run
    sc.traci._process.wait()
    with pytest.raises(SumoConnectionError) as e:
        sc.step()
    assert e.value.code == "CONNECTION_LOST"
    assert not sc.active          # state cleaned up, no dangling handle


@requires_sumo
def test_close_is_idempotent(smoke_cfg):
    sc = SumoConnection()
    sc.start(smoke_cfg)
    sc.close()
    sc.close()
    assert not sc.active