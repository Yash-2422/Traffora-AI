"""Configuration for the TrafficMind agent.

All paths and tunable values live here so nothing is hardcoded in the logic
modules. Thresholds for prediction and signals are added in later steps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

# backend/agents/traffic_mind/config.py -> parents[3] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent

SIM_ROOT = REPO_ROOT / "simulation" / "traffic_mind"
NETWORK_DIR = SIM_ROOT / "sample_network"
SCENARIO_DIR = SIM_ROOT / "scenarios"
DATASET_DIR = REPO_ROOT / "datasets" / "traffic_mind"
MODEL_DIR = PACKAGE_DIR / "models"

DEFAULT_SCENARIO = "mixed"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


@dataclass(frozen=True)
class SimulationConfig:
    """Settings for one SUMO run.

    ``scenario`` is the name of a ``.sumocfg`` in ``simulation/traffic_mind/scenarios``
    (built by ``simulation/traffic_mind/build_scenario.py``). ``sumocfg`` can
    point to any other config file and takes priority.
    """

    scenario: str = DEFAULT_SCENARIO
    sumocfg: Optional[Path] = None
    gui: bool = False
    gui_delay_ms: int = 100
    seed: int = 42
    demand_scale: float = 1.0
    step_length: float = 1.0
    end_time: int = 3600
    connect_retries: int = 3
    label: str = "traffic_mind"
    # Queue measurement (queue length in metres): vehicles slower than
    # ``queue_speed_mps`` whose spacing is at most ``queue_gap_m`` form one queue,
    # starting at the stop line; ``vehicle_length_m`` is added for the last car.
    # The speed limit is above SUMO's "halting" threshold (0.1 m/s) on purpose:
    # while a queue discharges, its first cars creep at 0.3-1 m/s and would
    # otherwise cut the queue short.
    queue_speed_mps: float = 1.5
    # The first slow vehicle may be this far from the stop line: cars at the head of
    # a discharging queue have already pulled away while the standing queue is behind.
    queue_head_tolerance_m: float = 40.0
    # A chain of at least this many slow cars is a queue even if it does not start near
    # the stop line (the cars in front of it are already moving: a discharging queue).
    queue_min_standing_vehicles: int = 3
    queue_gap_m: float = 10.0
    vehicle_length_m: float = 5.0

    def resolved_sumocfg(self) -> Path:
        if self.sumocfg is not None:
            return Path(self.sumocfg)
        return SCENARIO_DIR / f"{self.scenario}.sumocfg"


@dataclass(frozen=True)
class MonitorConfig:
    """Settings for the traffic monitor."""

    sample_interval_s: int = 5   # how often a snapshot is taken in the scripts
    flow_window_s: int = 60      # rolling window used for the flow estimate
    history_size: int = 720      # number of snapshots kept in memory


def configure_logging(level: int = logging.INFO) -> None:
    """Set up console logging (used by scripts and tests, not on import)."""
    logging.basicConfig(level=level, format=LOG_FORMAT)


@dataclass(frozen=True)
class CongestionConfig:
    """How measured traffic is turned into a congestion score and level.

    Each road gets four signals scaled to 0..1 (1 = as bad as the reference):

    * occupancy   = occupancy_pct / ``occupancy_ref_pct``
    * queue       = (queue_length_m / road length) / ``queue_ref_fraction``
    * waiting     = mean_waiting_time_s / ``waiting_ref_s``
    * speed drop  = 1 - speed_ratio

    The waiting and speed-drop signals are scaled down on roads with fewer than
    ``min_vehicles_full_weight`` vehicles, because one stopped car at a red light
    is not congestion. The score is the weighted sum (weights add up to 1).
    Score >= ``medium_threshold`` is MEDIUM, >= ``high_threshold`` is HIGH.

    A level only changes after the new condition has held for
    ``escalate_after_s`` (worse) or ``clear_after_s`` (better) seconds, so alerts
    do not flicker with every signal change.
    """

    weight_occupancy: float = 0.25
    weight_queue: float = 0.35
    weight_waiting: float = 0.20
    weight_speed_drop: float = 0.20

    # Reference values: "1.0" on a signal means this much. The sample network jams at
    # about 45 % occupancy; a queue over half the road risks blocking the previous
    # junction; one full signal cycle is 70 s (2 x (30 green + 3 yellow + 2 all-red)),
    # and waiting longer than a cycle means the queue did not clear on its green.
    occupancy_ref_pct: float = 45.0
    queue_ref_fraction: float = 0.5
    waiting_ref_s: float = 70.0
    min_vehicles_full_weight: int = 4

    # Queues build during red and clear during green, so a single sample swings with the
    # signal cycle. The four signals of each road are averaged over this many seconds
    # before they are scored: one full signal cycle (70 s) of the sample network.
    # 0 = score every sample as it is.
    smoothing_window_s: float = 70.0

    # Calibrated (on the smoothed score) with the four sample scenarios: off-peak traffic
    # is LOW 99 % of the time and never HIGH; the directional peaks and the surge reach
    # HIGH on 12-15 % of junction time, sustained heavy traffic is mostly LOW/MEDIUM.
    # Re-calibrate these if the network, signal timing or demand change a lot.
    medium_threshold: float = 0.25
    high_threshold: float = 0.50

    escalate_after_s: float = 15.0
    clear_after_s: float = 30.0
    # A junction that stays MEDIUM this long is flagged as needing attention
    # (a HIGH junction is flagged immediately).
    attention_medium_after_s: float = 60.0

    def __post_init__(self) -> None:
        weights = (self.weight_occupancy, self.weight_queue,
                   self.weight_waiting, self.weight_speed_drop)
        if any(w < 0 for w in weights) or abs(sum(weights) - 1.0) > 1e-6:
            raise ValueError(f"congestion weights must be >= 0 and add up to 1, got {weights}")
        if not 0 < self.medium_threshold < self.high_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 < medium < high <= 1")
        if min(self.occupancy_ref_pct, self.queue_ref_fraction, self.waiting_ref_s) <= 0:
            raise ValueError("reference values must be positive")
        if self.min_vehicles_full_weight < 1:
            raise ValueError("min_vehicles_full_weight must be >= 1")
        if min(self.escalate_after_s, self.clear_after_s, self.attention_medium_after_s,
               self.smoothing_window_s) < 0:
            raise ValueError("durations must be >= 0")


@dataclass(frozen=True)
class PredictionConfig:
    """Settings for traffic prediction (features, horizons, history).

    The model predicts, for every junction, the congestion level ``horizon_s`` seconds
    ahead. Features come from the junction's recent history: current metrics, how much
    they changed ``lags_s`` seconds ago, and a trend over ``trend_window_s`` seconds.
    A prediction needs ``min_history_s`` of history; before that the agent falls back to
    "the level stays as it is".
    """

    horizon_s: int = 60
    horizons_s: Tuple[int, ...] = (30, 60, 120)   # horizons the dataset provides labels for
    sample_interval_s: int = 5                    # seconds between snapshots
    lags_s: Tuple[int, ...] = (30, 60)
    trend_window_s: int = 60
    min_history_s: int = 60
    history_s: int = 120                          # history kept in memory
    random_state: int = 42

    def __post_init__(self) -> None:
        if self.sample_interval_s <= 0:
            raise ValueError("sample_interval_s must be positive")
        if not self.lags_s or min(self.lags_s) <= 0:
            raise ValueError("lags_s must be positive")
        if self.trend_window_s <= 0 or self.horizon_s <= 0:
            raise ValueError("trend_window_s and horizon_s must be positive")
        if self.horizon_s not in self.horizons_s:
            raise ValueError("horizon_s must be one of horizons_s")
        if self.min_history_s < max(max(self.lags_s), self.trend_window_s):
            raise ValueError("min_history_s must cover the longest lag and the trend window")
        if self.history_s < self.min_history_s:
            raise ValueError("history_s must be at least min_history_s")

    def model_path(self, horizon_s: Optional[int] = None) -> Path:
        """Where the trained model for a horizon is stored."""
        return MODEL_DIR / f"traffic_model_h{horizon_s or self.horizon_s}.joblib"