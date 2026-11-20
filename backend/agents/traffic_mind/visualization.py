"""Standalone Matplotlib dashboard for the TrafficMind agent.

Nothing here talks to SUMO. Feed a ``RunRecorder`` the agent's outputs (snapshot, congestion
report and, optionally, prediction and signal reports) once per sample, then draw:

* ``build_dashboard`` / ``save_dashboard`` - a static six-panel figure of a whole run,
* ``LiveDashboard``                         - a small window that updates while a run is going.

Panels of the static dashboard:

1. the 3x3 network map at one moment (level of every junction, forecast ring if it is expected
   to get worse),
2. vehicles in the network and stopped vehicles over time,
3. congestion score of every junction over time (heatmap),
4. recommended green shift of every junction over time (heatmap, diverging),
5. one junction's forecast against what actually happened,
6. which junctions spent the most time congested.

Colors follow a validated palette. Congestion levels use the fixed status colors and are
always also marked by a shape and a text label, so the figure reads without color vision.
Matplotlib only: the unified React dashboard is built at integration (Phase 2).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from pydantic import ValidationError

from .exceptions import InvalidTrafficDataError
from .schemas import (
    CongestionReport,
    PredictionReport,
    SignalRecommendationReport,
    TrafficSnapshot,
)

logger = logging.getLogger(__name__)

LEVELS = ("LOW", "MEDIUM", "HIGH")
_RANK = {level: i for i, level in enumerate(LEVELS)}

# --------------------------------------------------------------------------- #
# Look: tokens per theme (validated palette; see the dataviz notes in the docs)
# --------------------------------------------------------------------------- #
_SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
THEMES: Dict[str, Dict[str, Any]] = {
    "light": {
        "surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e", "grid": "#e6e5e1", "road": "#c9c8c2",
        "series1": "#2a78d6", "series2": "#eb6834", "mid": "#f0efec",
        "pole_pos": "#256abf", "pole_neg": "#e34948", "sequential": _SEQUENTIAL_BLUE,
    },
    "dark": {
        "surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7", "grid": "#2e2e2c", "road": "#4a4a46",
        "series1": "#3987e5", "series2": "#d95926", "mid": "#383835",
        "pole_pos": "#3987e5", "pole_neg": "#e66767", "sequential": _SEQUENTIAL_BLUE[::-1],
    },
}
# fixed status colors, same in both themes; always paired with a shape and a label
STATUS = {"LOW": "#0ca30c", "MEDIUM": "#fab219", "HIGH": "#d03b3b"}
MARKER = {"LOW": "o", "MEDIUM": "s", "HIGH": "^"}
LABEL = {"LOW": "Low", "MEDIUM": "Medium", "HIGH": "High"}


def _theme(name: str) -> Dict[str, Any]:
    try:
        return THEMES[name]
    except KeyError:
        raise ValueError(f"theme must be one of {sorted(THEMES)}, got {name!r}") from None


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #
def _validated(value: Any, model: Any, what: str) -> Any:
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except ValidationError as exc:
        raise InvalidTrafficDataError(f"Invalid {what}: {exc}") from exc


class RunRecorder:
    """Keeps the compact history the dashboard needs (not the full reports)."""

    def __init__(self, scenario: str = "") -> None:
        self.scenario = scenario
        self.horizon_s: Optional[int] = None
        self._network: List[Dict[str, float]] = []
        self._junctions: List[Dict[str, Any]] = []
        self._latest: List[Dict[str, Any]] = []

    def __len__(self) -> int:
        return len(self._network)

    def add(
        self,
        snapshot: Union[TrafficSnapshot, Dict[str, Any]],
        congestion: Union[CongestionReport, Dict[str, Any]],
        prediction: Optional[Union[PredictionReport, Dict[str, Any]]] = None,
        signals: Optional[Union[SignalRecommendationReport, Dict[str, Any]]] = None,
    ) -> None:
        """Record one sample. Reports must be from the same time as the snapshot.

        Raises:
            InvalidTrafficDataError: malformed input or reports from different times.
        """
        snap = _validated(snapshot, TrafficSnapshot, "snapshot")
        cong = _validated(congestion, CongestionReport, "congestion report")
        pred = _validated(prediction, PredictionReport, "prediction report") if prediction is not None else None
        sig = _validated(signals, SignalRecommendationReport, "signal report") if signals is not None else None
        for name, other in (("congestion report", cong), ("prediction report", pred), ("signal report", sig)):
            if other is not None and abs(other.timestamp_s - snap.timestamp_s) > 1e-6:
                raise InvalidTrafficDataError(
                    f"The {name} (t={other.timestamp_s}) is not from the snapshot's time (t={snap.timestamp_s}).")
        if self._network and snap.timestamp_s < self._network[-1]["time_s"]:
            raise InvalidTrafficDataError("Samples must be recorded in time order.")

        forecasts = ({p.junction_id: p for p in pred.predictions} if pred is not None and pred.method == "ml" else {})
        if forecasts:
            self.horizon_s = pred.horizon_s
        recs = {r.junction_id: r for r in sig.recommendations} if sig is not None else {}
        rows = []
        for j in cong.junctions:
            f, r = forecasts.get(j.junction_id), recs.get(j.junction_id)
            rows.append({
                "time_s": snap.timestamp_s, "junction_id": j.junction_id, "score": j.score,
                "level": j.level.value, "since_s": j.since_s,
                "predicted_level": f.predicted_level.value if f else None,
                "confidence": f.confidence if f else np.nan,
                "shift_s": (r.stages[0].recommended_green_s - r.stages[0].current_green_s) if r else np.nan,
                "action": r.action if r else None,
                "immediate": r.immediate.action if r else None,
                "priority": r.priority if r else None,
            })
        self._network.append({"time_s": snap.timestamp_s, "vehicles": snap.network.total_vehicles,
                              "stopped": snap.network.halting_vehicles,
                              "mean_wait_s": snap.network.mean_waiting_time_s})
        self._junctions.extend(rows)
        self._latest = rows

    # ------------------------------------------------------------------ #
    @property
    def junction_ids(self) -> List[str]:
        return sorted({r["junction_id"] for r in self._latest} or {r["junction_id"] for r in self._junctions})

    @property
    def latest_time_s(self) -> float:
        return self._network[-1]["time_s"] if self._network else 0.0

    @property
    def latest(self) -> List[Dict[str, Any]]:
        return list(self._latest)

    def network_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self._network, columns=["time_s", "vehicles", "stopped", "mean_wait_s"])

    def junction_frame(self) -> pd.DataFrame:
        columns = ["time_s", "junction_id", "score", "level", "since_s", "predicted_level", "confidence",
                   "shift_s", "action", "immediate", "priority"]
        return pd.DataFrame(self._junctions, columns=columns)

    def save_csv(self, directory: Union[str, Path]) -> List[Path]:
        """Write ``network.csv`` and ``junctions.csv`` (the data behind the figure)."""
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        paths = [folder / "network.csv", folder / "junctions.csv"]
        self.network_frame().to_csv(paths[0], index=False)
        self.junction_frame().to_csv(paths[1], index=False)
        return paths

    def forecast_accuracy(self, junction_id: str) -> Optional[Tuple[float, int]]:
        """(share of forecasts that matched the level reached ``horizon_s`` later, number checked)."""
        frame = self.junction_frame()
        frame = frame[frame["junction_id"] == junction_id]
        if self.horizon_s is None or frame["predicted_level"].isna().all():
            return None
        actual = frame.set_index("time_s")["level"]
        hits = total = 0
        for t, predicted in zip(frame["time_s"], frame["predicted_level"]):
            target = t + self.horizon_s
            if predicted is not None and isinstance(predicted, str) and target in actual.index:
                total += 1
                hits += predicted == actual.loc[target]
        return (hits / total, total) if total else None


# --------------------------------------------------------------------------- #
# Layout of the junction map
# --------------------------------------------------------------------------- #
def junction_positions(junction_ids: List[str]) -> Dict[str, Tuple[float, float]]:
    """Grid position of each junction. ``A0``..``C2`` -> column letter, row digit; others on a line."""
    pattern = re.compile(r"^([A-Za-z])(\d+)$")
    matches = {j: pattern.match(j) for j in junction_ids}
    if all(matches.values()):
        return {j: (float(ord(m.group(1).upper()) - ord("A")), float(int(m.group(2)))) for j, m in matches.items()}
    return {j: (float(i), 0.0) for i, j in enumerate(sorted(junction_ids))}


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #
def _style_axes(ax: Any, th: Dict[str, Any], grid: str = "y") -> None:
    ax.set_facecolor(th["surface"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(th["grid"])
    ax.tick_params(colors=th["text2"], labelsize=9, length=0)
    ax.xaxis.label.set_color(th["text2"])
    ax.yaxis.label.set_color(th["text2"])
    ax.title.set_color(th["text"])
    if grid:
        ax.grid(axis=grid, color=th["grid"], linewidth=0.8)
        ax.set_axisbelow(True)


def _title(ax: Any, text: str, th: Dict[str, Any], sub: str = "") -> None:
    ax.set_title(text, loc="left", fontsize=11, fontweight="bold", color=th["text"], pad=18 if sub else 8)
    if sub:
        ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=8.5, color=th["text2"], va="bottom")


def _empty(ax: Any, th: Dict[str, Any], message: str) -> None:
    ax.set_facecolor(th["surface"])
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.text(0.5, 0.5, message, ha="center", va="center", transform=ax.transAxes, fontsize=10, color=th["text2"])


def _level_legend(ax: Any, th: Dict[str, Any], loc: str = "lower right", ring: bool = False,
                  columns: int = 1) -> None:
    handles = [Line2D([0], [0], marker=MARKER[lv], linestyle="", markersize=9, markerfacecolor=STATUS[lv],
                      markeredgecolor=th["surface"], label=LABEL[lv]) for lv in LEVELS]
    if ring:
        handles.append(Line2D([0], [0], marker="o", linestyle="", markersize=11, markerfacecolor="none",
                              markeredgecolor=th["text2"], markeredgewidth=2, label="Ring: forecast worse"))
    legend = ax.legend(handles=handles, loc=loc, fontsize=8, frameon=False, labelcolor=th["text2"], ncol=columns)
    legend.set_zorder(5)


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
def draw_map(ax: Any, rows: List[Dict[str, Any]], time_s: float, th: Dict[str, Any]) -> None:
    """Network map: one marker per junction (color + shape + text = level)."""
    _style_axes(ax, th, grid="")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    _title(ax, "Network now", th, f"t = {time_s / 60:.0f} min · fill = level now, ring = forecast worse")
    if not rows:
        _empty(ax, th, "No data yet")
        return
    pos = junction_positions([r["junction_id"] for r in rows])
    ids = list(pos)
    for i, a in enumerate(ids):                      # roads between neighbouring junctions
        for b in ids[i + 1:]:
            if abs(pos[a][0] - pos[b][0]) + abs(pos[a][1] - pos[b][1]) == 1:
                ax.plot([pos[a][0], pos[b][0]], [pos[a][1], pos[b][1]], color=th["road"], linewidth=5,
                        solid_capstyle="round", zorder=1)
    any_forecast = False
    for r in rows:
        x, y = pos[r["junction_id"]]
        level = r["level"]
        predicted = r.get("predicted_level")
        if isinstance(predicted, str) and _RANK[predicted] > _RANK[level]:
            any_forecast = True
            ax.scatter([x], [y], s=1500, marker="o", facecolors="none", edgecolors=STATUS[predicted],
                       linewidths=3.5, zorder=2)
        ax.scatter([x], [y], s=720, marker=MARKER[level], color=STATUS[level], edgecolors=th["surface"],
                   linewidths=2, zorder=3)
        ax.text(x, y - 0.3, f"{r['junction_id']}  {LABEL[level]}", ha="center", va="top", fontsize=8.5,
                color=th["text"], zorder=4)
        ax.text(x, y - 0.46, f"score {r['score']:.2f}", ha="center", va="top", fontsize=7.5, color=th["text2"])
    xs, ys = [p[0] for p in pos.values()], [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 0.9, max(xs) + 0.9)
    ax.set_ylim(min(ys) - 1.45, max(ys) + 0.6)
    ax.set_aspect("equal")
    _level_legend(ax, th, loc="lower center", ring=any_forecast, columns=4)


def draw_network_series(ax: Any, network: pd.DataFrame, th: Dict[str, Any], window_s: Optional[float] = None) -> None:
    """Vehicles in the network and stopped vehicles over time (one axis)."""
    _style_axes(ax, th, grid="y")
    _title(ax, "Vehicles in the network", th, "all roads, every sample")
    if network.empty:
        _empty(ax, th, "No data yet")
        return
    frame = network if window_s is None else network[network["time_s"] >= network["time_s"].max() - window_s]
    t = frame["time_s"] / 60.0
    ax.plot(t, frame["vehicles"], color=th["series1"], linewidth=2, label="Vehicles")
    ax.plot(t, frame["stopped"], color=th["series2"], linewidth=2, label="Stopped")
    for column, color, label in (("vehicles", th["series1"], "Vehicles"), ("stopped", th["series2"], "Stopped")):
        ax.annotate(f"{label} {frame[column].iloc[-1]:.0f}", (t.iloc[-1], frame[column].iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", fontsize=8.5, color=th["text"], va="center",
                    annotation_clip=False)
    ax.set_xlabel("time (min)")
    ax.set_ylabel("vehicles")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", fontsize=8.5, frameon=False, labelcolor=th["text2"])
    ax.margins(x=0.01)


def _pivot(junctions: pd.DataFrame, value: str) -> Tuple[np.ndarray, List[float], List[str]]:
    table = junctions.pivot_table(index="junction_id", columns="time_s", values=value, aggfunc="first", dropna=False)
    table = table.sort_index()                         # A0 first = top row
    return table.to_numpy(dtype=float), list(table.columns), list(table.index)


def _heatmap(ax: Any, matrix: np.ndarray, times: List[float], ids: List[str], cmap: Any, norm: Any,
             th: Dict[str, Any]) -> Any:
    _style_axes(ax, th, grid="")
    step = (times[1] - times[0]) / 60.0 if len(times) > 1 else 0.25
    extent = (times[0] / 60.0 - step / 2, times[-1] / 60.0 + step / 2, -0.5, len(ids) - 0.5)
    image = ax.imshow(np.ma.masked_invalid(matrix), aspect="auto", cmap=cmap, norm=norm, extent=extent,
                      origin="upper", interpolation="nearest")
    ax.set_yticks(range(len(ids)))
    ax.set_yticklabels(ids[::-1])                      # ticks run bottom-to-top
    ax.set_xlabel("time (min)")
    cmap.set_bad(th["surface"])
    return image


def _colorbar(fig: Figure, image: Any, ax: Any, th: Dict[str, Any], ticks: List[float], labels: List[str]) -> None:
    bar = fig.colorbar(image, ax=ax, ticks=ticks, fraction=0.04, pad=0.02)
    bar.ax.set_yticklabels(labels)
    bar.ax.tick_params(colors=th["text2"], labelsize=8, length=0)
    bar.outline.set_visible(False)


def draw_congestion_heatmap(fig: Figure, ax: Any, junctions: pd.DataFrame, th: Dict[str, Any]) -> None:
    """Congestion score of every junction over time (sequential, one hue)."""
    _title(ax, "Congestion by junction", th, "score 0-1; medium from 0.25, high from 0.50")
    if junctions.empty:
        _empty(ax, th, "No data yet")
        return
    matrix, times, ids = _pivot(junctions, "score")
    cmap = LinearSegmentedColormap.from_list("traffic_seq", th["sequential"])
    image = _heatmap(ax, matrix, times, ids, cmap, None, th)
    image.set_clim(0.0, 1.0)
    _colorbar(fig, image, ax, th, [0, 0.25, 0.5, 1.0], ["0", "0.25 medium", "0.50 high", "1"])


def draw_signal_heatmap(fig: Figure, ax: Any, junctions: pd.DataFrame, th: Dict[str, Any],
                        limit_s: float = 10.0) -> None:
    """Recommended green shift toward each junction's first stage (diverging, gray = no change)."""
    _title(ax, "Recommended green shift", th, "seconds moved to stage 1 (blue) or stage 2 (red) per cycle")
    if junctions.empty or junctions["shift_s"].isna().all():
        _empty(ax, th, "No signal recommendations recorded")
        return
    matrix, times, ids = _pivot(junctions, "shift_s")
    cmap = LinearSegmentedColormap.from_list("traffic_div", [th["pole_neg"], th["mid"], th["pole_pos"]])
    image = _heatmap(ax, matrix, times, ids, cmap, TwoSlopeNorm(vmin=-limit_s, vcenter=0.0, vmax=limit_s), th)
    _colorbar(fig, image, ax, th, [-limit_s, 0, limit_s], [f"-{limit_s:.0f} s", "0", f"+{limit_s:.0f} s"])


def draw_forecast(ax: Any, recorder: RunRecorder, junction_id: str, th: Dict[str, Any]) -> None:
    """One junction: level reached (blue) against what was forecast for that moment (orange)."""
    horizon = recorder.horizon_s
    frame = recorder.junction_frame()
    frame = frame[frame["junction_id"] == junction_id]
    _style_axes(ax, th, grid="y")
    accuracy = recorder.forecast_accuracy(junction_id)
    headline = f"Forecast vs actual, junction {junction_id}"
    if accuracy:
        headline += f"  -  {accuracy[0]:.0%} of {accuracy[1]} forecasts matched"
    _title(ax, headline, th, "each forecast is drawn at the moment it is about"
           + (f" ({horizon} s ahead)" if horizon else ""))
    if frame.empty:
        _empty(ax, th, "No data yet")
        return
    if horizon is None or frame["predicted_level"].isna().all():
        _empty(ax, th, "No forecasts recorded\n(no trained model, or too little history)")
        return
    actual = frame.set_index("time_s")["level"].map(_RANK)
    ax.step(actual.index / 60.0, actual.to_numpy(), where="post", color=th["series1"], linewidth=2.2, label="Actual level")
    forecast = frame.dropna(subset=["predicted_level"])
    ax.step((forecast["time_s"] + horizon) / 60.0, forecast["predicted_level"].map(_RANK).to_numpy() + 0.07,
            where="post", color=th["series2"], linewidth=1.8, label=f"Forecast made {horizon} s earlier")
    ax.set_yticks([0, 1, 2])
    ax.set_yticklabels([LABEL[lv] for lv in LEVELS])
    ax.set_ylim(-0.3, 2.4)
    ax.set_xlabel("time (min)")
    ax.legend(loc="upper left", fontsize=8.5, frameon=False, labelcolor=th["text2"], ncol=2)


def draw_time_congested(ax: Any, junctions: pd.DataFrame, th: Dict[str, Any]) -> None:
    """Share of the run each junction spent at Medium and High (ranked, stacked, status colors)."""
    _style_axes(ax, th, grid="x")
    _title(ax, "Time spent congested", th, "share of the run, ranked")
    if junctions.empty:
        _empty(ax, th, "No data yet")
        return
    shares = (junctions.groupby("junction_id")["level"].value_counts(normalize=True).unstack(fill_value=0.0)
              .reindex(columns=list(LEVELS), fill_value=0.0) * 100.0)
    shares = shares.sort_values(["HIGH", "MEDIUM"], ascending=True)
    y = np.arange(len(shares))
    gap = 0.25                                           # a small gap keeps the two fills apart
    ax.barh(y, shares["MEDIUM"], height=0.62, color=STATUS["MEDIUM"], label="Medium", edgecolor="none")
    ax.barh(y, shares["HIGH"], height=0.62, left=shares["MEDIUM"] + gap, color=STATUS["HIGH"], label="High",
            edgecolor="none")
    ax.set_yticks(y)
    ax.set_yticklabels(shares.index)
    for yi, (_, row) in zip(y[-3:], shares.iloc[-3:].iterrows()):
        total = row["MEDIUM"] + row["HIGH"]
        if total >= 0.5:
            ax.text(total + gap + 0.8, yi, f"{row['HIGH']:.0f}% high · {row['MEDIUM']:.0f}% medium", va="center",
                    fontsize=8.5, color=th["text"])
    ax.set_xlabel("% of the run")
    ax.set_xlim(0, max(10.0, float((shares["MEDIUM"] + shares["HIGH"]).max()) * 1.6))
    handles = [Line2D([0], [0], marker=MARKER[lv], linestyle="", markersize=8, markerfacecolor=STATUS[lv],
                      markeredgecolor="none", label=LABEL[lv]) for lv in ("MEDIUM", "HIGH")]
    ax.legend(handles=handles, loc="lower right", fontsize=8.5, frameon=False, labelcolor=th["text2"])


# --------------------------------------------------------------------------- #
# Static dashboard
# --------------------------------------------------------------------------- #
def busiest_junction(recorder: RunRecorder) -> Optional[str]:
    """Junction with the most time at High, then Medium (the most interesting one to plot)."""
    frame = recorder.junction_frame()
    if frame.empty:
        return None
    score = frame.assign(high=frame["level"] == "HIGH", med=frame["level"] == "MEDIUM").groupby("junction_id")[
        ["high", "med"]].sum()
    return str(score.sort_values(["high", "med"], ascending=False).index[0])


def build_dashboard(
    recorder: RunRecorder,
    junction: Optional[str] = None,
    at_s: Optional[float] = None,
    theme: str = "light",
    title: Optional[str] = None,
) -> Figure:
    """The static six-panel dashboard for everything recorded so far.

    Args:
        junction: junction for the forecast panel (default: the busiest one).
        at_s: time shown on the map (default: the latest sample).
        theme: "light" or "dark".

    Raises:
        InvalidTrafficDataError: nothing has been recorded, or an unknown junction was named.
        ValueError: unknown theme.
    """
    th = _theme(theme)
    if len(recorder) == 0:
        raise InvalidTrafficDataError("Nothing recorded yet: call RunRecorder.add() first.")
    frame = recorder.junction_frame()
    if junction is None:
        junction = busiest_junction(recorder)
    elif junction not in set(frame["junction_id"]):
        raise InvalidTrafficDataError(f"Unknown junction {junction!r}; choose from {recorder.junction_ids}.")

    if at_s is None:
        at_s = recorder.latest_time_s
    times = sorted(frame["time_s"].unique())
    shown = min(times, key=lambda t: abs(t - at_s))
    map_rows = frame[frame["time_s"] == shown].to_dict("records")

    fig = Figure(figsize=(15, 12.5), facecolor=th["surface"], layout="constrained")
    grid = fig.add_gridspec(3, 2, height_ratios=[1.05, 1, 1])
    draw_map(fig.add_subplot(grid[0, 0]), map_rows, shown, th)
    draw_network_series(fig.add_subplot(grid[0, 1]), recorder.network_frame(), th)
    draw_congestion_heatmap(fig, fig.add_subplot(grid[1, 0]), frame, th)
    draw_signal_heatmap(fig, fig.add_subplot(grid[1, 1]), frame, th)
    draw_forecast(fig.add_subplot(grid[2, 0]), recorder, junction, th)
    draw_time_congested(fig.add_subplot(grid[2, 1]), frame, th)
    heading = title or f"TrafficMind - {recorder.scenario or 'simulation'}"
    fig.suptitle(f"{heading}   ({times[0] / 60:.0f}-{times[-1] / 60:.0f} min, {len(recorder)} samples)",
                 x=0.01, ha="left", fontsize=14, fontweight="bold", color=th["text"])
    return fig


def save_dashboard(recorder: RunRecorder, path: Union[str, Path], dpi: int = 150, **kwargs: Any) -> Path:
    """Draw the dashboard and save it as PNG (or any format Matplotlib knows from the extension)."""
    fig = build_dashboard(recorder, **kwargs)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=dpi, facecolor=fig.get_facecolor())
    return target


# --------------------------------------------------------------------------- #
# Live view
# --------------------------------------------------------------------------- #
class LiveDashboard:
    """A small window (map, last 10 minutes, advice) that is redrawn while a run goes on.

    ``show=True`` opens an interactive window (needs a GUI backend; on Windows the default works).
    ``show=False`` keeps the figure off screen so ``save_frame`` can write PNGs (used by tests).
    Call ``refresh()`` after ``recorder.add()``; ``close()`` when done.
    """

    WINDOW_S = 600.0

    def __init__(self, recorder: RunRecorder, theme: str = "light", show: bool = True) -> None:
        self.recorder = recorder
        self.theme = _theme(theme)
        self.show = show
        self._plt: Any = None
        if show:
            import matplotlib.pyplot as plt

            self._plt = plt
            plt.ion()
            self.fig = plt.figure(figsize=(12, 6.5), facecolor=self.theme["surface"], layout="constrained")
            try:
                self.fig.canvas.manager.set_window_title("TrafficMind - live")
            except Exception:  # noqa: BLE001 - not every backend has a window title
                pass
        else:
            self.fig = Figure(figsize=(12, 6.5), facecolor=self.theme["surface"], layout="constrained")
        grid = self.fig.add_gridspec(2, 2, width_ratios=[1.1, 1], height_ratios=[1.3, 1])
        self.ax_map = self.fig.add_subplot(grid[:, 0])
        self.ax_series = self.fig.add_subplot(grid[0, 1])
        self.ax_text = self.fig.add_subplot(grid[1, 1])

    def refresh(self) -> None:
        """Redraw from the recorder's latest data."""
        th, rec = self.theme, self.recorder
        for ax in (self.ax_map, self.ax_series, self.ax_text):
            ax.clear()
        draw_map(self.ax_map, rec.latest, rec.latest_time_s, th)
        draw_network_series(self.ax_series, rec.network_frame(), th, window_s=self.WINDOW_S)
        self._draw_advice()
        if self.show:
            self.fig.canvas.draw_idle()
            self._plt.pause(0.001)

    def _draw_advice(self) -> None:
        th, ax = self.theme, self.ax_text
        _style_axes(ax, th, grid="")
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        _title(ax, "Signal advice (recommend only)", th)
        order = {"high": 0, "medium": 1, "low": 2, "none": 3, None: 4}
        rows = sorted(self.recorder.latest, key=lambda r: (order.get(r["priority"], 4), -r["score"], r["junction_id"]))
        shown = [r for r in rows if r["action"] == "rebalance" or r["immediate"] in ("extend_green", "end_green_early")]
        if not shown:
            ax.text(0.0, 0.9, "No change recommended", transform=ax.transAxes, fontsize=10, color=th["text2"], va="top")
            return
        for i, r in enumerate(shown[:6]):
            shift = r["shift_s"]
            move = ("" if not isinstance(shift, float) or np.isnan(shift) or abs(shift) < 1
                    else f"green {abs(shift):.0f} s to stage {1 if shift > 0 else 2}")
            now = {"extend_green": "extend current green", "end_green_early": "end current green early"}.get(r["immediate"], "")
            text = f"{r['junction_id']}  {LABEL[r['level']]:<6}  " + "; ".join(p for p in (move, now) if p)
            ax.text(0.0, 0.92 - i * 0.15, text, transform=ax.transAxes, fontsize=9.5, family="monospace",
                    color=th["text"], va="top")

    def save_frame(self, path: Union[str, Path], dpi: int = 100) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.fig.savefig(target, dpi=dpi, facecolor=self.fig.get_facecolor())
        return target

    def close(self) -> None:
        if self._plt is not None:
            self._plt.ioff()
            self._plt.close(self.fig)