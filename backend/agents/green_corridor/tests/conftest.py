import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from backend.agents.green_corridor.connection import find_binary  # noqa: E402

requires_sumo = pytest.mark.skipif(
    find_binary("sumo") is None or find_binary("netgenerate") is None,
    reason="SUMO not installed (set SUMO_HOME or pip install eclipse-sumo)",
)


@pytest.fixture
def smoke_cfg(tmp_path):
    """A tiny real SUMO scenario: 3x3 signalised grid + one ambulance."""
    import sumolib
    net = tmp_path / "t.net.xml"
    subprocess.run(
        [find_binary("netgenerate"), "--grid", "--grid.number", "3", "--grid.length", "200",
         "--tls.guess", "true", "--tls.guess.threshold", "0", "-o", str(net)],
        check=True, capture_output=True)
    edges = [e for e in sumolib.net.readNet(str(net)).getEdges() if e.allows("emergency")]
    (tmp_path / "t.rou.xml").write_text(
        f'<routes><vType id="ambulance" vClass="emergency"/>'
        f'<trip id="amb_1" type="ambulance" depart="0" from="{edges[0].getID()}" to="{edges[-1].getID()}"/></routes>')
    cfg = tmp_path / "t.sumocfg"
    cfg.write_text('<configuration><input><net-file value="t.net.xml"/>'
                   '<route-files value="t.rou.xml"/></input>'
                   '<time><begin value="0"/><end value="200"/></time></configuration>')
    return cfg


# ---- shared fixtures for detection / tracking tests (Step 4) ----------------------------------
@pytest.fixture(scope="session")
def scenario(tmp_path_factory):
    """A small but real scenario: signalised 5x5 grid, 40 cars, ambulance departing at t=20 s."""
    from backend.agents.green_corridor.scenario_builder import ScenarioSpec, build_scenario
    d = tmp_path_factory.mktemp("gc_scenario")
    spec = ScenarioSpec(background_vehicles=40, background_window_s=100,
                        ambulance_depart_s=20, sim_end_s=600)
    return build_scenario(spec, d / "net", d / "scn", d / "res")


@pytest.fixture
def sim(scenario):
    """A running baseline simulation plus an EmergencyDetector bound to it."""
    from backend.agents.green_corridor.connection import SumoConnection
    from backend.agents.green_corridor.emergency_detector import EmergencyDetector
    sc = SumoConnection()
    sc.start(scenario.baseline_cfg)
    try:
        yield sc, EmergencyDetector(sc)
    finally:
        sc.close()