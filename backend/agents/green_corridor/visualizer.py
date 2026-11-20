"""GreenCorridor standalone visualisation (Step 9).

Reads the measured results written by the Step 8 comparison
(simulation/green_corridor/results/comparison.json) plus the SUMO network file,
and draws a GreenCorridor-only dashboard. It never starts SUMO and invents no numbers.

Limitation: the ambulance track is reconstructed by interpolating between the
measured junction timestamps. It is exact at junctions, approximate between them.

Usage (repo root):
    python -m backend.agents.green_corridor.visualizer
    python -m backend.agents.green_corridor.visualizer --interactive
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("green_corridor.visualizer")

REPO_ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = REPO_ROOT / "simulation" / "green_corridor"
DEFAULT_RESULTS = SIM_DIR / "results" / "comparison.json"
DEFAULT_OUT = SIM_DIR / "results" / "dashboard.png"
BASELINE_CFG = SIM_DIR / "scenarios" / "baseline.sumocfg"


# --------------------------------------------------------------------------- loading
def load_results(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Results file not found: {path}. Run the Step 8 comparison first.")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    for mode in ("baseline", "priority"):
        if mode not in data or "metrics" not in data[mode]:
            raise ValueError(f"Results file has no '{mode}.metrics' section.")
    if not data.get("completed_both", False):
        logger.warning("completed_both is false: one run did not finish, numbers may be partial.")
    return data


def find_net_file(explicit: Optional[Path] = None) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(f"Network file not found: {explicit}")
        return explicit
    if BASELINE_CFG.exists():
        try:
            node = ET.parse(BASELINE_CFG).getroot().find(".//net-file")
            if node is not None:
                candidate = (BASELINE_CFG.parent / node.get("value")).resolve()
                if candidate.exists():
                    return candidate
        except ET.ParseError as exc:
            logger.warning("Could not parse %s: %s", BASELINE_CFG, exc)
    found = sorted(SIM_DIR.rglob("*.net.xml"))
    if not found:
        raise FileNotFoundError(f"No .net.xml found under {SIM_DIR}")
    return found[0]


def load_network(net_file: Path) -> Dict[str, Any]:
    root = ET.parse(net_file).getroot()
    edges: Dict[str, np.ndarray] = {}
    for e in root.iter("edge"):
        if e.get("function") == "internal":
            continue
        lane = e.find("lane")
        if lane is None or not lane.get("shape"):
            continue
        pts = [tuple(float(v) for v in p.split(",")[:2]) for p in lane.get("shape").split()]
        edges[e.get("id")] = np.array(pts)
    junctions = {
        j.get("id"): (float(j.get("x")), float(j.get("y")))
        for j in root.iter("junction")
        if j.get("type") != "internal" and j.get("x") is not None
    }
    if not edges:
        raise ValueError(f"No drivable edges found in {net_file}")
    return {"edges": edges, "junctions": junctions}


# --------------------------------------------------------------------------- route / track
def route_edges_from_result(result: Dict[str, Any]) -> List[str]:
    """Route rebuilt from the measured crossings (approach edges + final exit edge)."""
    crossings = result["baseline"]["metrics"].get("crossings", [])
    if not crossings:
        raise ValueError("No crossings in results; cannot rebuild the route.")
    return [c["approach_edge"] for c in crossings] + [crossings[-1]["exit_edge"]]


def build_route_geometry(net: Dict[str, Any], edges: List[str]) -> Dict[str, Any]:
    missing = [e for e in edges if e not in net["edges"]]
    if missing:
        raise ValueError(f"Route edges not in network file: {missing}")
    poly = np.vstack([net["edges"][e] for e in edges])
    seg = np.hypot(*np.diff(poly, axis=0).T)
    vertex_s = np.concatenate([[0.0], np.cumsum(seg)])
    ends: List[float] = []
    n = 0
    for e in edges:
        n += len(net["edges"][e])
        ends.append(float(vertex_s[n - 1]))
    return {"poly": poly, "vertex_s": vertex_s, "edge_end_s": ends, "edges": edges}


def reconstruct_track(metrics: Dict[str, Any], geom: Dict[str, Any]) -> Dict[str, np.ndarray]:
    """Elapsed time -> (x, y), interpolated between measured junction timestamps."""
    edge_idx = {e: i for i, e in enumerate(geom["edges"])}
    t0, t1 = metrics["depart_time_s"], metrics["arrival_time_s"]
    ts, ss = [float(t0)], [0.0]
    for c in metrics.get("crossings", []):
        t = c["entered_time_s"] if c.get("entered_time_s") is not None else c["crossed_time_s"]
        s = geom["edge_end_s"][edge_idx[c["approach_edge"]]]
        if t > ts[-1] and s >= ss[-1]:
            ts.append(float(t))
            ss.append(s)
    if t1 > ts[-1]:
        ts.append(float(t1))
        ss.append(float(geom["vertex_s"][-1]))
    times = np.arange(t0, t1 + 1.0, 1.0)
    s_t = np.interp(times, ts, ss)
    x = np.interp(s_t, geom["vertex_s"], geom["poly"][:, 0])
    y = np.interp(s_t, geom["vertex_s"], geom["poly"][:, 1])
    return {"elapsed": times - t0, "x": x, "y": y}


# --------------------------------------------------------------------------- priority actions
def parse_priority_actions(result: Dict[str, Any]) -> List[Tuple[float, str, str]]:
    """(time_s, tls_id, kind) from the measured priority log; kind is EXTEND/TRUNCATE/RESTORE."""
    pr = result["priority"]
    actions = (pr.get("priority") or {}).get("actions", [])
    crossings = pr["metrics"].get("crossings", [])
    out: List[Tuple[float, str, str]] = []
    for a in actions:
        reason = a.get("reason", "")
        call = a.get("call", "")
        if call == "restore_normal_operation" and crossings:
            nearest = min(crossings, key=lambda c: abs(c["crossed_time_s"] - a["time_s"]))
            out.append((a["time_s"], nearest["tls_id"], "RESTORE"))
            continue
        m = re.search(r"\(([^,)]+)", call)
        if not m:
            continue
        if reason.startswith("extend"):
            out.append((a["time_s"], m.group(1), "EXTEND"))
        elif reason.startswith("truncate"):
            out.append((a["time_s"], m.group(1), "TRUNCATE"))
        # "restore this phase's normal programmed duration" is bookkeeping; not plotted.
    return out


# --------------------------------------------------------------------------- drawing
def _draw_map(ax, net, geom, crossings, title):
    for pts in net["edges"].values():
        ax.plot(pts[:, 0], pts[:, 1], color="#cfcfcf", lw=1.2, zorder=1)
    poly = geom["poly"]
    ax.plot(poly[:, 0], poly[:, 1], color="#1f77b4", lw=3.2, zorder=2)
    for c in crossings:
        xy = net["junctions"].get(c["tls_id"])
        if xy is None:
            continue
        wait = c.get("wait_time_s", 0.0)
        ax.scatter([xy[0]], [xy[1]], s=150, color="#d62728" if wait > 0 else "#2ca02c",
                   edgecolor="black", zorder=4)
        label = c["tls_id"] + (f"\nwait {wait:.0f}s" if wait > 0 else "")
        ax.annotate(label, xy, textcoords="offset points", xytext=(0, 11), ha="center", fontsize=7)
    ax.scatter([poly[0, 0]], [poly[0, 1]], marker="o", s=90, color="white",
               edgecolor="black", zorder=5)
    ax.annotate("Start", poly[0], textcoords="offset points", xytext=(0, -14),
                ha="center", fontsize=8)
    ax.scatter([poly[-1, 0]], [poly[-1, 1]], marker="*", s=380, color="gold",
               edgecolor="black", zorder=5)
    ax.annotate("Hospital", poly[-1], textcoords="offset points", xytext=(0, -16),
                ha="center", fontsize=8, fontweight="bold")
    ax.set_aspect("equal")
    ax.set_title(title, fontsize=11)
    ax.axis("off")


def _kpi_text(result):
    b, p = result["baseline"]["metrics"], result["priority"]["metrics"]
    pri = result["priority"].get("priority") or {}
    errors = result["priority"].get("priority_errors", [])
    lines = [
        f"Vehicle: {result.get('vehicle_id', '?')}   Seed: {result.get('seed', '?')}",
        f"respect_min_dur: {result.get('respect_min_dur')}",
        "",
        "                  normal -> priority",
        f"Journey time      {b['journey_time_s']:.0f} s -> {p['journey_time_s']:.0f} s",
        f"Time saved        {result.get('time_saved_s', 0):.0f} s ({result.get('time_saved_pct', 0):.1f}%)",
        f"Junction wait     {b['intersection_wait_time_s']:.0f} s -> {p['intersection_wait_time_s']:.0f} s",
        f"Average speed     {b['average_speed_kmh']:.1f} -> {p['average_speed_kmh']:.1f} km/h",
        f"Distance          {b['distance_travelled_m']:.0f} m -> {p['distance_travelled_m']:.0f} m",
        f"Intersections     {b['signalised_intersections_crossed']} -> "
        f"{p['signalised_intersections_crossed']} crossed",
        "",
        f"Priority requests {pri.get('requests', 0)}  {pri.get('by_state', {})}",
        f"Signal changes    {pri.get('signal_interventions', 0)} "
        f"({pri.get('extensions', 0)} extend, {pri.get('truncations', 0)} truncate)",
        f"Priority errors   {len(errors)}",
    ]
    return "\n".join(lines)


def build_dashboard(result, net, geom, interactive=False):
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    b, p = result["baseline"]["metrics"], result["priority"]["metrics"]
    fig = plt.figure(figsize=(17, 9.5))
    gs = fig.add_gridspec(2, 3, left=0.04, right=0.98, top=0.90,
                          bottom=0.12 if interactive else 0.07,
                          hspace=0.28, wspace=0.18, height_ratios=[1.25, 1])
    fig.suptitle(
        f"GreenCorridor: ambulance {result.get('vehicle_id', '')} | "
        f"{result.get('time_saved_s', 0):.0f} s saved ({result.get('time_saved_pct', 0):.1f}%) "
        f"with signal priority (measured in SUMO)", fontsize=14, fontweight="bold")

    ax_b, ax_p, ax_k = (fig.add_subplot(gs[0, i]) for i in range(3))
    _draw_map(ax_b, net, geom, b["crossings"],
              f"Normal signals: {b['journey_time_s']:.0f} s journey")
    _draw_map(ax_p, net, geom, p["crossings"],
              f"Emergency priority: {p['journey_time_s']:.0f} s journey")
    ax_k.axis("off")
    ax_k.text(0.0, 1.0, _kpi_text(result), va="top", ha="left", family="monospace", fontsize=10)
    ax_k.legend(handles=[
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#d62728", markeredgecolor="k",
               markersize=9, label="Ambulance waited here"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#2ca02c", markeredgecolor="k",
               markersize=9, label="No wait"),
        Line2D([0], [0], color="#1f77b4", lw=3, label="Emergency route"),
    ], loc="lower left", fontsize=8, frameon=False)

    ax1 = fig.add_subplot(gs[1, 0])
    ids = [c["tls_id"] for c in b["crossings"]]
    pw = {c["tls_id"]: c["wait_time_s"] for c in p["crossings"]}
    xs = np.arange(len(ids))
    ax1.bar(xs - 0.2, [c["wait_time_s"] for c in b["crossings"]], 0.4, label="Normal", color="#d62728")
    ax1.bar(xs + 0.2, [pw.get(i, 0) for i in ids], 0.4, label="Priority", color="#2ca02c")
    ax1.set_xticks(xs, ids)
    ax1.set_ylabel("Wait (s)")
    ax1.set_title("Wait at each intersection")
    ax1.legend(fontsize=8)

    ax2 = fig.add_subplot(gs[1, 1])
    labels = ["Journey time", "Stopped time"]
    bv = [b["journey_time_s"], b["stopped_time_s"]]
    pv = [p["journey_time_s"], p["stopped_time_s"]]
    xs2 = np.arange(len(labels))
    r1 = ax2.bar(xs2 - 0.2, bv, 0.4, label="Normal", color="#d62728")
    r2 = ax2.bar(xs2 + 0.2, pv, 0.4, label="Priority", color="#2ca02c")
    ax2.bar_label(r1, fmt="%.0f", fontsize=8)
    ax2.bar_label(r2, fmt="%.0f", fontsize=8)
    ax2.set_xticks(xs2, labels)
    ax2.set_ylabel("Seconds")
    ax2.set_title("Normal vs priority (measured)")
    ax2.legend(fontsize=8)

    ax3 = fig.add_subplot(gs[1, 2])
    styles = {"EXTEND": ("^", "#1f77b4", "Extend green"),
              "TRUNCATE": ("v", "#ff7f0e", "Truncate conflicting green"),
              "RESTORE": ("o", "#2ca02c", "Restore normal")}
    order = {t: i for i, t in enumerate(ids)}
    actions = parse_priority_actions(result)
    for kind, (mk, col, lab) in styles.items():
        pts = [(t, order[tl]) for t, tl, k in actions if k == kind and tl in order]
        if pts:
            ax3.scatter(*zip(*pts), marker=mk, color=col, s=55, label=lab, edgecolor="k", zorder=3)
    ax3.set_yticks(range(len(ids)), ids)
    ax3.invert_yaxis()
    ax3.set_xlabel("Simulation time (s)")
    ax3.set_title("Signal priority actions")
    ax3.grid(alpha=0.3)
    ax3.legend(fontsize=7, loc="upper left")

    if interactive:
        from matplotlib.widgets import Slider
        tb = reconstruct_track(b, geom)
        tp = reconstruct_track(p, geom)
        mb, = ax_b.plot([tb["x"][0]], [tb["y"][0]], "o", ms=13, color="red",
                        markeredgecolor="k", zorder=6)
        mp, = ax_p.plot([tp["x"][0]], [tp["y"][0]], "o", ms=13, color="red",
                        markeredgecolor="k", zorder=6)
        tmax = float(max(tb["elapsed"][-1], tp["elapsed"][-1]))
        sax = fig.add_axes([0.2, 0.03, 0.6, 0.03])
        slider = Slider(sax, "Elapsed (s)", 0.0, tmax, valinit=0.0, valstep=1.0)

        def update(t):
            for m, tr in ((mb, tb), (mp, tp)):
                m.set_data([np.interp(t, tr["elapsed"], tr["x"])],
                           [np.interp(t, tr["elapsed"], tr["y"])])
            fig.canvas.draw_idle()

        slider.on_changed(update)
        fig._gc_slider = slider  # keep a reference so it is not garbage-collected
    return fig


# --------------------------------------------------------------------------- CLI
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="GreenCorridor standalone dashboard")
    ap.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    ap.add_argument("--net", type=Path, default=None, help="SUMO .net.xml (auto-detected)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--interactive", action="store_true", help="open a window with a time slider")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import matplotlib
    if not args.interactive:
        matplotlib.use("Agg")
    try:
        result = load_results(args.results)
        net_file = find_net_file(args.net)
        net = load_network(net_file)
        geom = build_route_geometry(net, route_edges_from_result(result))
        fig = build_dashboard(result, net, geom, interactive=args.interactive)
    except (FileNotFoundError, ValueError, KeyError, ET.ParseError) as exc:
        logger.error("Cannot build dashboard: %s", exc)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=150)
    logger.info("Saved %s (network: %s)", args.out, net_file.name)
    if args.interactive:
        import matplotlib.pyplot as plt
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())