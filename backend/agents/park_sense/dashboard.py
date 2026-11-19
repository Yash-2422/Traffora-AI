"""ParkSense standalone dashboard (Matplotlib).

Each plot_* function draws on a Matplotlib Axes you give it, so the main
TRAFORRA dashboard can reuse any panel. build_dashboard() puts them together.

Run from the project root:
    python -m backend.agents.park_sense.dashboard --demo
    python -m backend.agents.park_sense.dashboard --sumo
    python -m backend.agents.park_sense.dashboard --demo --save parksense_dashboard.png --no-show
"""

import argparse
import logging
from datetime import datetime
from typing import List, Optional

import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from . import config
from .agent import ParkSenseAgent
from .schemas import ParkingZone

logger = logging.getLogger(__name__)

DEMAND_COLORS = {"Low": "#2e9e5b", "Medium": "#f0a030", "High": "#d9453d", "Unknown": "#9aa0a6"}
OCCUPIED_COLOR = "#d9453d"
FREE_COLOR = "#2e9e5b"


def _short_label(zone: dict) -> str:
    return f"{zone['zone_id']}\n{zone['name'].replace(' Parking', '')}"


# ---------- panels ----------
def draw_summary(ax, analytics: dict) -> None:
    """Text panel with the headline numbers."""
    ax.axis("off")
    most = analytics["most_occupied_zone"]
    rec = analytics["recommended_zone"]
    rec_text = f"{rec['name']} ({rec['zone_id']})" if rec else "none available"
    lines = [
        f"Total capacity: {analytics['total_capacity']}      "
        f"Occupied: {analytics['total_occupied']}      "
        f"Available: {analytics['total_available']}      "
        f"Overall occupancy: {analytics['overall_occupancy_pct']}%",
        f"City demand: {analytics['city_demand_level']}      "
        f"Most occupied: {most['name']} ({most['occupancy_pct']}%)",
        f"Recommended for {analytics['destination']['name']}: {rec_text}",
    ]
    ax.text(0.01, 0.5, "\n".join(lines), va="center", ha="left", fontsize=12,
            family="monospace", transform=ax.transAxes,
            bbox=dict(boxstyle="round,pad=0.6", facecolor="#f3f6fb", edgecolor="#c5d0e0"))


def plot_capacity(ax, analytics: dict) -> None:
    """Occupied vs free spaces per zone."""
    zones = analytics["zones"]
    labels = [_short_label(z).replace("\n", "  ") for z in zones]
    occupied = [z["occupied"] for z in zones]
    available = [z["available"] for z in zones]
    ax.barh(labels, occupied, color=OCCUPIED_COLOR, label="Occupied")
    ax.barh(labels, available, left=occupied, color=FREE_COLOR, label="Available")
    for i, z in enumerate(zones):
        ax.text(z["capacity"] + 0.2, i, f"{z['occupied']}/{z['capacity']}", va="center", fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("Parking spaces")
    ax.set_title("Capacity by zone")
    ax.legend(loc="lower right", fontsize=8)
    ax.set_xlim(0, max(z["capacity"] for z in zones) * 1.15)


def plot_occupancy_demand(ax, analytics: dict) -> None:
    """Current occupancy %, coloured by predicted demand level."""
    zones = analytics["zones"]
    labels = [_short_label(z) for z in zones]
    values = [z["occupancy_pct"] for z in zones]
    colors = [DEMAND_COLORS.get(z["demand_level"], DEMAND_COLORS["Unknown"]) for z in zones]
    bars = ax.bar(labels, values, color=colors)
    for bar, v in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, v + 1.5, f"{v}%", ha="center", fontsize=9)
    ax.axhline(config.DEMAND_MEDIUM_THRESHOLD, color="gray", linestyle="--", linewidth=1)
    ax.axhline(config.DEMAND_HIGH_THRESHOLD, color="gray", linestyle="--", linewidth=1)
    ax.set_ylim(0, 110)
    ax.set_ylabel("Current occupancy %")
    ax.set_title("Occupancy now (colour = predicted demand)")
    ax.tick_params(axis="x", labelsize=8)
    ax.legend(handles=[Patch(color=c, label=f"{k} demand") for k, c in DEMAND_COLORS.items() if k != "Unknown"],
              loc="upper right", fontsize=8)


def plot_current_vs_predicted(ax, analytics: dict) -> None:
    """Occupancy now compared with the prediction."""
    zones = analytics["zones"]
    n = len(zones)
    width = 0.38
    xs = list(range(n))
    current = [z["occupancy_pct"] for z in zones]
    predicted = [z["predicted_occupancy_pct"] if z["predicted_occupancy_pct"] is not None else 0 for z in zones]
    ax.bar([x - width / 2 for x in xs], current, width, color="#4a78c2", label="Now")
    ax.bar([x + width / 2 for x in xs], predicted, width, color="#8e5ac2",
           label=f"In {config.PREDICTION_HORIZON_MINUTES} min")
    for x, p in zip(xs, predicted):
        ax.text(x + width / 2, p + 1.5, f"{p:.0f}", ha="center", fontsize=8)
    ax.set_xticks(xs)
    ax.set_xticklabels([_short_label(z) for z in zones], fontsize=8)
    ax.set_ylim(0, 110)
    ax.set_ylabel("Occupancy %")
    ax.set_title("Demand prediction")
    ax.legend(fontsize=8, loc="upper right")


def plot_map(ax, analytics: dict, zones: List[ParkingZone], recommendation: Optional[dict] = None) -> None:
    """Map of zones (size = capacity, colour = occupancy), the destination and the ranking."""
    meta = {z.zone_id: z for z in zones}
    rows = [z for z in analytics["zones"] if z["zone_id"] in meta]
    xs = [meta[r["zone_id"]].x for r in rows]
    ys = [meta[r["zone_id"]].y for r in rows]
    dest = analytics["destination"]

    sc = ax.scatter(xs, ys, s=[r["capacity"] * 45 for r in rows], c=[r["occupancy_pct"] for r in rows],
                    cmap="RdYlGn_r", vmin=0, vmax=100, edgecolors="black", zorder=3)
    for r, x, y in zip(rows, xs, ys):
        ax.annotate(f"{r['zone_id']}\n{r['available']} free", (x, y), textcoords="offset points",
                    xytext=(0, 20), ha="center", fontsize=8)

    ax.scatter([dest["x"]], [dest["y"]], marker="P", s=260, c="navy", zorder=4)
    ax.annotate(dest["name"], (dest["x"], dest["y"]), textcoords="offset points",
                xytext=(0, -22), ha="center", fontsize=9, color="navy", fontweight="bold")

    if recommendation:
        for rec in recommendation.get("recommendations", []):
            zone = meta.get(rec["zone_id"])
            if zone is None:
                continue
            if rec["rank"] == 1:
                ax.scatter([zone.x], [zone.y], s=900, facecolors="none", edgecolors="gold",
                           linewidths=3, zorder=2)
            ax.annotate(f"#{rec['rank']}", (zone.x, zone.y), textcoords="offset points",
                        xytext=(0, -24), ha="center", fontsize=9, color="white", fontweight="bold",
                        bbox=dict(boxstyle="round,pad=0.25", facecolor="#1f4e9c"), zorder=5)

    all_x = xs + [dest["x"]]
    all_y = ys + [dest["y"]]
    pad = 120
    ax.set_xlim(min(all_x) - pad, max(all_x) + pad)
    ax.set_ylim(min(all_y) - pad, max(all_y) + pad)
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title("City map (colour = occupancy, gold ring = best, #n = rank)")
    ax.grid(True, alpha=0.3)
    ax.figure.colorbar(sc, ax=ax, label="Occupancy %", shrink=0.8)


# ---------- the full dashboard ----------
def build_dashboard(analytics: dict, zones: List[ParkingZone], recommendation: Optional[dict] = None):
    """Create the dashboard figure from agent outputs.

    Input:  analytics (from get_parking_analytics), zones (agent.zones),
            recommendation (from recommend_parking, optional)
    Output: a Matplotlib Figure
    """
    fig = plt.figure(figsize=(15, 9), constrained_layout=True)
    grid = fig.add_gridspec(3, 2, height_ratios=[0.4, 1, 1])
    fig.suptitle("TRAFORRA AI - ParkSense Dashboard", fontsize=16, fontweight="bold")
    draw_summary(fig.add_subplot(grid[0, :]), analytics)
    plot_capacity(fig.add_subplot(grid[1, 0]), analytics)
    plot_occupancy_demand(fig.add_subplot(grid[1, 1]), analytics)
    plot_current_vs_predicted(fig.add_subplot(grid[2, 0]), analytics)
    plot_map(fig.add_subplot(grid[2, 1]), analytics, zones, recommendation)
    return fig


# ---------- data sources ----------
def _sample_data():
    agent = ParkSenseAgent()
    agent.update_occupancy({"PA_A": 5, "PA_B": 3, "PA_C": 2, "PA_D": 12, "PA_E": 1})
    now = datetime(2026, 11, 24, 9, 30)
    return agent, now


def _sumo_data(stop_time: int = 300):
    import sumolib
    import traci

    agent = ParkSenseAgent()
    traci.start([sumolib.checkBinary("sumo"), "-c", str(config.SUMO_CONFIG_FILE),
                 "--no-step-log", "true", "--no-warnings", "true"])
    try:
        while traci.simulation.getTime() < stop_time:
            traci.simulationStep()
            agent.update_from_sumo(traci.simulation.getTime())
    finally:
        traci.close()
    return agent, datetime.now()


def main() -> None:
    parser = argparse.ArgumentParser(description="ParkSense standalone dashboard")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="use sample occupancy")
    source.add_argument("--sumo", action="store_true", help="use live SUMO data (t = 300 s)")
    parser.add_argument("--save", metavar="FILE", help="save the dashboard as a PNG image")
    parser.add_argument("--no-show", action="store_true", help="do not open a window")
    args = parser.parse_args()
    config.setup_logging(logging.WARNING)

    if args.no_show:
        plt.switch_backend("Agg")

    agent, now = _sumo_data() if args.sumo else _sample_data()
    recommendation = agent.recommend_parking("hospital", now=now)
    analytics = agent.get_parking_analytics("hospital", now=now)
    fig = build_dashboard(analytics, list(agent.zones), recommendation)

    if args.save:
        fig.savefig(args.save, dpi=150)
        print(f"Dashboard saved to {args.save}")
    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()