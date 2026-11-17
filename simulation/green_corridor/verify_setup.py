"""Step 2 verification: checks Python, SUMO, TraCI and a real TraCI round trip.

Run from the repo root:   python simulation/green_corridor/verify_setup.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.agents.green_corridor import config
from backend.agents.green_corridor.connection import (
    SumoConnection, SumoConnectionError, find_binary, find_sumo_home,
)

OK, FAIL = "[ OK ]", "[FAIL]"


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"{OK if passed else FAIL} {label}" + (f"  ->  {detail}" if detail else ""))
    return passed


def build_smoke_scenario(tmp: Path) -> Path:
    """Generate a tiny 3x3 signalised grid with one ambulance, using netgenerate."""
    netgen = find_binary("netgenerate")
    net = tmp / "smoke.net.xml"
    subprocess.run(
        [netgen, "--grid", "--grid.number", "3", "--grid.attach-length", "0", "--grid.length", "200",
         "--default.junctions.keep-clear", "false", "--tls.guess", "true", "--tls.guess.threshold", "0", "-o", str(net)],
        check=True, capture_output=True,
    )
    # Edge ids come from netgenerate: first node-to-node edge in the grid
    import sumolib
    network = sumolib.net.readNet(str(net))
    edges = [e for e in network.getEdges() if e.allows("emergency")]
    origin, dest = edges[0], edges[-1]
    (tmp / "smoke.rou.xml").write_text(f"""<routes>
  <vType id="ambulance" vClass="emergency" accel="3.5" decel="5.0" maxSpeed="25" guiShape="emergency"/>
  <trip id="amb_smoke" type="ambulance" depart="0" from="{origin.getID()}" to="{dest.getID()}"/>
</routes>""")
    cfg = tmp / "smoke.sumocfg"
    cfg.write_text("""<configuration>
  <input><net-file value="smoke.net.xml"/><route-files value="smoke.rou.xml"/></input>
  <time><begin value="0"/><end value="300"/></time>
</configuration>""")
    return cfg


def main() -> int:
    results = []
    results.append(check("Python >= 3.9", sys.version_info >= (3, 9), sys.version.split()[0]))

    home = find_sumo_home()
    results.append(check("SUMO_HOME resolved", home is not None, str(home)))
    for b in ("sumo", "netgenerate"):
        path = find_binary(b)
        results.append(check(f"binary '{b}' found", path is not None, str(path)))
    if not all(results):
        print("\nSetup incomplete. See README Windows instructions.")
        return 1

    try:
        import traci, sumolib  # noqa: F401
        results.append(check("import traci + sumolib", True))
    except ImportError as exc:
        results.append(check("import traci + sumolib", False, str(exc)))
        return 1

    with tempfile.TemporaryDirectory() as td:
        try:
            cfg = build_smoke_scenario(Path(td))
            results.append(check("netgenerate built a signalised grid", True))
            with SumoConnection(gui=False) as sc:
                sc.start(cfg)
                results.append(check("TraCI connected", sc.active))
                tls = sc.traci.trafficlight.getIDList()
                results.append(check("traffic lights present", len(tls) > 0, f"{len(tls)} signals"))
                seen = None
                for _ in range(60):
                    sc.step()
                    if "amb_smoke" in sc.traci.vehicle.getIDList():
                        seen = "amb_smoke"
                        break
                results.append(check("ambulance vehicle appears in SUMO", seen is not None))
                if seen:
                    v = sc.traci
                    print("       vClass:", v.vehicle.getVehicleClass(seen),
                          "| edge:", v.vehicle.getRoadID(seen),
                          "| speed:", round(v.vehicle.getSpeed(seen), 2))
                    results.append(check("vehicle class is 'emergency'",
                                         v.vehicle.getVehicleClass(seen) == "emergency"))
        except (SumoConnectionError, subprocess.CalledProcessError) as exc:
            results.append(check("TraCI round trip", False, str(exc)))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())