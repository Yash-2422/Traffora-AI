"""Build the TrafficMind sample network and demand scenarios.

Creates (inside ``simulation/traffic_mind``):

* ``sample_network/grid3x3.net.xml``  - 3x3 grid of signalised junctions with
  dead-end entry/exit roads on every border.
* ``scenarios/<profile>.rou.xml``     - time-varying demand (flows).
* ``scenarios/<profile>.sumocfg``     - SUMO configuration for the profile.

Usage (from the repository root)::

    python simulation/traffic_mind/build_scenario.py
    python simulation/traffic_mind/build_scenario.py --profiles mixed peak --force-network

This script only needs SUMO (``netgenerate``) and ``sumolib``. It does not
import anything from the TrafficMind package, so it can be run on its own.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("traffic_mind.build_scenario")

SIM_DIR = Path(__file__).resolve().parent
NETWORK_DIR = SIM_DIR / "sample_network"
SCENARIO_DIR = SIM_DIR / "scenarios"
NET_FILE = NETWORK_DIR / "grid3x3.net.xml"

# Signal timing used when generating the network (seconds).
GREEN_TIME = 30
YELLOW_TIME = 3
ALL_RED_TIME = 2

SIM_DURATION = 3600  # seconds

OPPOSITE = {"west": "east", "east": "west", "north": "south", "south": "north"}
ADJACENT = {
    "west": ("north", "south"),
    "east": ("north", "south"),
    "north": ("west", "east"),
    "south": ("west", "east"),
}
SIDES = ("west", "east", "north", "south")

# Share of each entry's traffic going straight on / turning to each side.
THROUGH_SHARE = 0.6
TURN_SHARE = 0.2


# --------------------------------------------------------------------------- #
# Demand profiles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    """A period of constant demand.

    ``rate`` is vehicles/hour per entry road, by side of the network.
    ``overrides`` maps an entry node id (e.g. ``"left1"``) to its own rate,
    which is how "incident-like" surges are created.
    """

    begin: int
    end: int
    label: str
    rate: Dict[str, int]
    overrides: Dict[str, int] = field(default_factory=dict)


def _uniform(vph: int) -> Dict[str, int]:
    return {side: vph for side in SIDES}


PROFILES: Dict[str, List[Window]] = {
    # Light traffic everywhere - mostly "Low" congestion.
    "offpeak": [Window(0, SIM_DURATION, "offpeak", _uniform(120))],
    # Sustained heavy traffic for the full hour (just below gridlock).
    "peak": [Window(0, SIM_DURATION, "peak", _uniform(250))],
    # One hour with changing conditions. Default scenario for the agent.
    # Congestion builds up during the two directional peaks and the surge,
    # then clears during the recovery window.
    "mixed": [
        Window(0, 600, "low", _uniform(85)),
        Window(600, 1500, "eastwest_peak",
               {"west": 380, "east": 380, "north": 125, "south": 125}),
        Window(1500, 2400, "northsouth_peak",
               {"west": 125, "east": 125, "north": 380, "south": 380}),
        Window(2400, 3000, "surge", _uniform(140), {"left1": 630}),
        Window(3000, SIM_DURATION, "recovery", _uniform(85)),
    ],
    # Calm network that gets hit by a localised surge (incident-like).
    "surge": [
        Window(0, 1200, "baseline", _uniform(150)),
        Window(1200, 2400, "surge", _uniform(150),
               {"left1": 900, "bottom1": 800}),
        Window(2400, SIM_DURATION, "recovery", _uniform(150)),
    ],
}


# --------------------------------------------------------------------------- #
# SUMO tool discovery (Windows friendly)
# --------------------------------------------------------------------------- #
def find_sumo_tool(name: str) -> Optional[str]:
    """Locate a SUMO executable via SUMO_HOME, PATH, or the eclipse-sumo wheel."""
    exe = name + (".exe" if os.name == "nt" else "")
    sumo_home = os.environ.get("SUMO_HOME")
    if sumo_home:
        candidate = Path(sumo_home) / "bin" / exe
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    try:  # pip install eclipse-sumo
        import sumo  # type: ignore

        candidate = Path(sumo.SUMO_HOME) / "bin" / exe
        if candidate.is_file():
            return str(candidate)
    except Exception:  # noqa: BLE001 - optional fallback
        pass
    return None


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
def build_network(size: int = 3, force: bool = False) -> Path:
    """Generate the grid network with netgenerate (skipped if it exists)."""
    if NET_FILE.exists() and not force:
        logger.info("Network already exists: %s (use --force-network to rebuild)", NET_FILE)
        return NET_FILE

    netgenerate = find_sumo_tool("netgenerate")
    if netgenerate is None:
        raise RuntimeError(
            "netgenerate not found. Install SUMO and make sure SUMO_HOME is set "
            "(restart VS Code after installing)."
        )

    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    # Grid junctions are named A0..C2; only those get traffic lights
    # (the dead-end border nodes stay uncontrolled).
    tls_nodes = ",".join(
        f"{chr(ord('A') + i)}{j}" for i in range(size) for j in range(size)
    )
    cmd = [
        netgenerate,
        "--grid",
        f"--grid.x-number={size}",
        f"--grid.y-number={size}",
        "--grid.length=250",
        "--grid.attach-length=250",
        "--default-junction-type=priority",
        f"--tls.set={tls_nodes}",
        "--default.lanenumber=1",
        "--default.speed=13.89",
        f"--tls.green.time={GREEN_TIME}",
        f"--tls.yellow.time={YELLOW_TIME}",
        f"--tls.allred.time={ALL_RED_TIME}",
        "--no-turnarounds=true",
        "-o", str(NET_FILE),
    ]
    logger.info("Running netgenerate ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not NET_FILE.exists():
        raise RuntimeError(f"netgenerate failed:\n{result.stdout}\n{result.stderr}")
    logger.info("Network written: %s", NET_FILE)
    return NET_FILE


# --------------------------------------------------------------------------- #
# Demand
# --------------------------------------------------------------------------- #
def _node_side(node, bounds) -> Optional[str]:
    x, y = node.getCoord()
    min_x, max_x, min_y, max_y = bounds
    if x == min_x:
        return "west"
    if x == max_x:
        return "east"
    if y == min_y:
        return "south"
    if y == max_y:
        return "north"
    return None


def _collect_border_edges(net):
    """Return (entries, exits) as lists of (edge, side)."""
    coords = [n.getCoord() for n in net.getNodes()]
    bounds = (
        min(c[0] for c in coords), max(c[0] for c in coords),
        min(c[1] for c in coords), max(c[1] for c in coords),
    )
    entries, exits = [], []
    for edge in net.getEdges():
        src, dst = edge.getFromNode(), edge.getToNode()
        if src.getType() == "dead_end" and dst.getType() != "dead_end":
            side = _node_side(src, bounds)
            if side:
                entries.append((edge, side))
        elif dst.getType() == "dead_end" and src.getType() != "dead_end":
            side = _node_side(dst, bounds)
            if side:
                exits.append((edge, side))
    if not entries or not exits:
        raise RuntimeError("Could not find border entry/exit edges in the network.")
    return entries, exits


def _pick_destinations(entry_edge, entry_side, exits, rng):
    """Choose (exit_edge, share) pairs: one straight-through, two turning."""
    ex, ey = entry_edge.getFromNode().getCoord()
    by_side: Dict[str, list] = {s: [] for s in SIDES}
    for edge, side in exits:
        by_side[side].append(edge)

    # Straight through: exit on the opposite side in the same row/column.
    opposite = by_side[OPPOSITE[entry_side]]
    straight = opposite[0]
    for edge in opposite:
        cx, cy = edge.getToNode().getCoord()
        same_line = (cy == ey) if entry_side in ("west", "east") else (cx == ex)
        if same_line:
            straight = edge
            break

    result = [(straight, THROUGH_SHARE)]
    for side in ADJACENT[entry_side]:
        result.append((rng.choice(by_side[side]), TURN_SHARE))
    return result


def build_routes(profile: str, seed: int = 7) -> Path:
    """Write ``scenarios/<profile>.rou.xml`` for the given profile."""
    import sumolib  # imported here so --help works without SUMO tools

    net = sumolib.net.readNet(str(NET_FILE))
    entries, exits = _collect_border_edges(net)
    rng = random.Random(seed)
    destinations = {
        edge.getID(): _pick_destinations(edge, side, exits, rng) for edge, side in entries
    }

    root = ET.Element("routes")
    ET.SubElement(
        root, "vType", id="car", vClass="passenger", length="5.0", minGap="2.5",
        accel="2.6", decel="4.5", sigma="0.5", tau="1.0", maxSpeed="13.89",
    )

    flows = []
    for window in PROFILES[profile]:
        for edge, side in entries:
            node_id = edge.getFromNode().getID()
            vph = window.overrides.get(node_id, window.rate[side])
            for k, (dest, share) in enumerate(destinations[edge.getID()]):
                probability = vph * share / 3600.0
                flows.append((
                    window.begin,
                    {
                        "id": f"{window.label}_{edge.getID()}_{k}",
                        "type": "car",
                        "begin": str(window.begin),
                        "end": str(window.end),
                        "probability": f"{probability:.6f}",
                        "from": edge.getID(),
                        "to": dest.getID(),
                        "departLane": "best",
                        "departSpeed": "max",
                    },
                ))
    for _, attrs in sorted(flows, key=lambda item: item[0]):
        ET.SubElement(root, "flow", attrs)

    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    out = SCENARIO_DIR / f"{profile}.rou.xml"
    ET.indent(root)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    logger.info("Routes written: %s (%d flows)", out, len(flows))
    return out


def build_sumocfg(profile: str) -> Path:
    """Write ``scenarios/<profile>.sumocfg``."""
    net_rel = Path(os.path.relpath(NET_FILE, SCENARIO_DIR)).as_posix()
    root = ET.Element("configuration")
    inp = ET.SubElement(root, "input")
    ET.SubElement(inp, "net-file", value=net_rel)
    ET.SubElement(inp, "route-files", value=f"{profile}.rou.xml")
    time_el = ET.SubElement(root, "time")
    ET.SubElement(time_el, "begin", value="0")
    ET.SubElement(time_el, "end", value=str(SIM_DURATION))
    ET.SubElement(time_el, "step-length", value="1")
    proc = ET.SubElement(root, "processing")
    # Unblock real gridlock after 5 minutes instead of freezing forever.
    ET.SubElement(proc, "time-to-teleport", value="300")
    # Waiting time is accumulated over a 5 minute window (used by the monitor).
    ET.SubElement(proc, "waiting-time-memory", value="300")

    out = SCENARIO_DIR / f"{profile}.sumocfg"
    ET.indent(root)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    logger.info("Config written: %s", out)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build TrafficMind SUMO scenarios.")
    parser.add_argument(
        "--profiles", nargs="+", default=list(PROFILES), choices=list(PROFILES),
        help="Demand profiles to build (default: all).",
    )
    parser.add_argument("--seed", type=int, default=7, help="Seed for destination choice.")
    parser.add_argument("--force-network", action="store_true", help="Rebuild the network.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        build_network(force=args.force_network)
        for profile in args.profiles:
            build_routes(profile, seed=args.seed)
            build_sumocfg(profile)
    except (RuntimeError, ImportError) as exc:
        logger.error("%s", exc)
        return 1
    logger.info("Done. Scenarios available: %s", ", ".join(args.profiles))
    return 0


if __name__ == "__main__":
    sys.exit(main())