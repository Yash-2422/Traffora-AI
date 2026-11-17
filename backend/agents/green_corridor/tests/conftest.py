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