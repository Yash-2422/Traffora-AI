"""Builds a reproducible SUMO scenario: signalised grid city + background traffic + one ambulance.

Everything is generated from a ScenarioSpec, so scenarios are configurable and the output
files are deterministic for a given spec (same seed -> byte-identical route files).

Run:  python -m backend.agents.green_corridor.scenario_builder --help
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from . import config
from .connection import find_binary

logger = logging.getLogger(__name__)


class ScenarioError(Exception):
    """Invalid scenario specification or build failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ScenarioSpec:
    """All tunable scenario parameters. Edge ids follow netgenerate: node 'A0' -> 'A1' is edge 'A0A1'."""
    name: str = "default"
    grid_size: int = 5                       # junctions per side
    block_length_m: float = 300.0
    speed_limit_mps: float = 13.89           # 50 km/h
    lanes: int = 1
    seed: int = config.DEFAULT_SEED
    background_vehicles: int = 300
    background_window_s: float = 600.0       # background vehicles depart uniformly in [0, window]
    ambulance_id: str = "amb_1"
    ambulance_depart_s: float = 120.0        # after traffic has built up
    ambulance_speed_factor: float = 1.0
    origin_edge: str = "A0A1"
    hospital_edge: str = "E3E4"
    sim_end_s: float = 1800.0
    tls_cycle_s: int = 60                    # fixed-time signal cycle for the normal program

    def validate(self) -> None:
        if self.grid_size < 3 or self.grid_size > 10:
            raise ScenarioError("INVALID_SPEC", "grid_size must be between 3 and 10")
        if self.block_length_m <= 0 or self.speed_limit_mps <= 0:
            raise ScenarioError("INVALID_SPEC", "block_length_m and speed_limit_mps must be positive")
        if self.background_vehicles < 0 or self.background_window_s <= 0:
            raise ScenarioError("INVALID_SPEC", "background traffic parameters invalid")
        if self.ambulance_depart_s < 0 or self.sim_end_s <= self.ambulance_depart_s:
            raise ScenarioError("INVALID_SPEC", "ambulance_depart_s must be >= 0 and < sim_end_s")
        if self.origin_edge == self.hospital_edge:
            raise ScenarioError("INVALID_SPEC", "origin and hospital edge must differ")
        if self.tls_cycle_s < 30:
            raise ScenarioError("INVALID_SPEC", "tls_cycle_s must be >= 30 s")


@dataclass
class ScenarioFiles:
    """Paths and metadata of a built scenario (JSON-serialisable via to_dict)."""
    spec: dict
    net_file: str
    vtypes_file: str
    background_routes_file: str
    ambulance_routes_file: str
    hospital_file: str
    baseline_cfg: str
    priority_cfg: str
    baseline_tripinfo: str
    priority_tripinfo: str
    tls_ids: List[str] = field(default_factory=list)
    hospital_xy: List[float] = field(default_factory=list)
    reference_route_edges: List[str] = field(default_factory=list)  # static shortest path, sanity only
    reference_route_length_m: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _node_ids(n: int) -> List[str]:
    return [f"{chr(ord('A') + x)}{y}" for x in range(n) for y in range(n)]


def _signalised_nodes(n: int) -> List[str]:
    """Every junction except the four corners (corners are only 2-way bends)."""
    last = chr(ord("A") + n - 1)
    corners = {"A0", f"A{n - 1}", f"{last}0", f"{last}{n - 1}"}
    return [nid for nid in _node_ids(n) if nid not in corners]


def _rel(target: Path, start: Path) -> str:
    return os.path.relpath(target, start).replace("\\", "/")


def _generate_network(spec: ScenarioSpec, net_path: Path) -> None:
    netgen = find_binary("netgenerate")
    if netgen is None:
        raise ScenarioError("SUMO_NOT_FOUND", "netgenerate not found; install SUMO / pip install eclipse-sumo")
    cmd = [
        netgen, "--grid", "--grid.number", str(spec.grid_size),
        "--grid.length", str(spec.block_length_m),
        "--default.lanenumber", str(spec.lanes),
        "--default.speed", str(spec.speed_limit_mps),
        "--tls.set", ",".join(_signalised_nodes(spec.grid_size)),
        "--tls.cycle.time", str(spec.tls_cycle_s),
        "--tls.default-type", "static",
        "-o", str(net_path),
    ]
    logger.info("netgenerate: %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ScenarioError("NETWORK_BUILD_FAILED", f"netgenerate failed: {res.stderr.strip()[:500]}")


def _write_vtypes(path: Path, spec: ScenarioSpec) -> None:
    path.write_text(f"""<additional>
    <vType id="car" vClass="passenger" accel="2.6" decel="4.5" sigma="0.5" length="4.5" maxSpeed="{spec.speed_limit_mps * 1.2:.2f}" color="150,150,150"/>
    <vType id="{config.AMBULANCE_VTYPE_ID}" vClass="emergency" accel="3.5" decel="5.0" sigma="0.2" length="6.0"
           maxSpeed="{spec.speed_limit_mps * 1.5:.2f}" speedFactor="{spec.ambulance_speed_factor}"
           guiShape="emergency" color="255,0,0"/>
</additional>
""")


def _write_background(path: Path, spec: ScenarioSpec, edges: List[str]) -> None:
    rng = random.Random(spec.seed)  # own RNG: deterministic and independent of SUMO's seed
    departures = sorted(rng.uniform(0, spec.background_window_s) for _ in range(spec.background_vehicles))
    lines = ["<routes>"]
    for i, t in enumerate(departures):
        src, dst = rng.sample(edges, 2)
        lines.append(f'    <trip id="bg_{i}" type="car" depart="{t:.2f}" from="{src}" to="{dst}" departLane="best"/>')
    lines.append("</routes>")
    path.write_text("\n".join(lines) + "\n")


def _write_ambulance(path: Path, spec: ScenarioSpec) -> None:
    path.write_text(f"""<routes>
    <trip id="{spec.ambulance_id}" type="{config.AMBULANCE_VTYPE_ID}" depart="{spec.ambulance_depart_s:.2f}"
          from="{spec.origin_edge}" to="{spec.hospital_edge}" departLane="best" departSpeed="max"/>
</routes>
""")


def _write_hospital(path: Path, x: float, y: float) -> None:
    path.write_text(f"""<additional>
    <poi id="hospital" type="hospital" color="255,0,0" layer="10" x="{x:.2f}" y="{y:.2f}" width="40" height="40"/>
</additional>
""")


def _write_cfg(path: Path, files: dict, spec: ScenarioSpec, tripinfo_rel: str) -> None:
    d = path.parent
    path.write_text(f"""<configuration>
    <input>
        <net-file value="{_rel(files['net'], d)}"/>
        <route-files value="{_rel(files['bg'], d)},{_rel(files['amb'], d)}"/>
        <additional-files value="{_rel(files['vtypes'], d)},{_rel(files['hospital'], d)}"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="{spec.sim_end_s:.0f}"/>
    </time>
    <processing>
        <time-to-teleport value="-1"/>
    </processing>
    <output>
        <tripinfo-output value="{tripinfo_rel}"/>
    </output>
</configuration>
""")


def build_scenario(
    spec: Optional[ScenarioSpec] = None,
    network_dir: Path = config.NETWORK_DIR,
    scenario_dir: Path = config.SCENARIO_DIR,
    results_dir: Path = config.RESULTS_DIR,
) -> ScenarioFiles:
    """Generate network + traffic + ambulance + SUMO configs. Raises ScenarioError on invalid input."""
    spec = spec or ScenarioSpec()
    spec.validate()
    for d in (network_dir, scenario_dir, results_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    network_dir, scenario_dir, results_dir = Path(network_dir), Path(scenario_dir), Path(results_dir)

    net_path = network_dir / "grid.net.xml"
    _generate_network(spec, net_path)

    try:
        import sumolib
    except ImportError as exc:
        raise ScenarioError("TRACI_NOT_INSTALLED", "sumolib missing: pip install sumolib") from exc
    net = sumolib.net.readNet(str(net_path), withInternal=False)

    # --- validate ambulance endpoints against the REAL network
    for label, eid in (("origin_edge", spec.origin_edge), ("hospital_edge", spec.hospital_edge)):
        if not net.hasEdge(eid):
            raise ScenarioError("UNKNOWN_EDGE", f"{label} '{eid}' does not exist in the generated network")
    origin, hospital = net.getEdge(spec.origin_edge), net.getEdge(spec.hospital_edge)
    if not origin.allows("emergency") or not hospital.allows("emergency"):
        raise ScenarioError("EDGE_NOT_ACCESSIBLE", "origin/hospital edge does not allow emergency vehicles")
    path_edges, cost = net.getShortestPath(origin, hospital, vClass="emergency")
    if not path_edges:
        raise ScenarioError("NO_ROUTE", f"No route from {spec.origin_edge} to {spec.hospital_edge}")

    passenger_edges = [e.getID() for e in net.getEdges() if e.allows("passenger")]
    reserved = {spec.origin_edge, spec.hospital_edge}
    bg_edges = [e for e in passenger_edges if e not in reserved]

    vtypes, bg, amb, hosp = (scenario_dir / n for n in
                             ("vtypes.add.xml", "background.rou.xml", "ambulance.rou.xml", "hospital.add.xml"))
    _write_vtypes(vtypes, spec)
    _write_background(bg, spec, bg_edges)
    _write_ambulance(amb, spec)
    hx, hy = hospital.getToNode().getCoord()
    _write_hospital(hosp, hx, hy)

    parts = {"net": net_path, "bg": bg, "amb": amb, "vtypes": vtypes, "hospital": hosp}
    base_cfg, prio_cfg = scenario_dir / "baseline.sumocfg", scenario_dir / "priority.sumocfg"
    base_trip, prio_trip = results_dir / "baseline_tripinfo.xml", results_dir / "priority_tripinfo.xml"
    _write_cfg(base_cfg, parts, spec, _rel(base_trip, scenario_dir))
    _write_cfg(prio_cfg, parts, spec, _rel(prio_trip, scenario_dir))

    files = ScenarioFiles(
        spec=asdict(spec), net_file=str(net_path), vtypes_file=str(vtypes),
        background_routes_file=str(bg), ambulance_routes_file=str(amb), hospital_file=str(hosp),
        baseline_cfg=str(base_cfg), priority_cfg=str(prio_cfg),
        baseline_tripinfo=str(base_trip), priority_tripinfo=str(prio_trip),
        tls_ids=sorted(t.getID() for t in net.getTrafficLights()),
        hospital_xy=[hx, hy],
        reference_route_edges=[e.getID() for e in path_edges],
        reference_route_length_m=round(sum(e.getLength() for e in path_edges), 1),
    )
    (scenario_dir / config.SCENARIO_META_FILENAME).write_text(json.dumps(files.to_dict(), indent=2))
    logger.info("Scenario '%s' built: %d signals, reference route %d edges / %.0f m",
                spec.name, len(files.tls_ids), len(path_edges), files.reference_route_length_m)
    return files


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Build the GreenCorridor SUMO scenario")
    p.add_argument("--grid-size", type=int, default=5)
    p.add_argument("--background-vehicles", type=int, default=300)
    p.add_argument("--seed", type=int, default=config.DEFAULT_SEED)
    p.add_argument("--origin-edge", default="A0A1")
    p.add_argument("--hospital-edge", default="E3E4")
    p.add_argument("--ambulance-depart", type=float, default=120.0)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        files = build_scenario(ScenarioSpec(
            grid_size=a.grid_size, background_vehicles=a.background_vehicles, seed=a.seed,
            origin_edge=a.origin_edge, hospital_edge=a.hospital_edge, ambulance_depart_s=a.ambulance_depart))
    except ScenarioError as exc:
        print(f"[FAIL] {exc.code}: {exc.message}")
        return 1
    print(json.dumps({k: files.to_dict()[k] for k in
                      ("baseline_cfg", "priority_cfg", "tls_ids", "reference_route_edges",
                       "reference_route_length_m")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())