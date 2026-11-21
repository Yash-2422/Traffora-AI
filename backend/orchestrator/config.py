"""Configuration for the Traffora-AI Central Orchestrator.

Everything tunable lives here: paths, timing, safety limits and the decision
thresholds. No machine-specific absolute paths are hardcoded; all paths are
derived from the repository root.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

# backend/orchestrator/config.py -> parents[0]=orchestrator, [1]=backend, [2]=repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_DIR = Path(__file__).resolve().parent

SIMULATION_DIR = REPO_ROOT / "simulation" / "orchestrator"
DEFAULT_SUMOCFG = SIMULATION_DIR / "unified.sumocfg"
DEFAULT_ZONES_FILE = SIMULATION_DIR / "parking_data" / "zones.json"

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


@dataclass(frozen=True)
class SafetyLimits:
    """Hard limits enforced by :class:`SafetyValidator`.

    These are intentionally conservative and independent of any agent's own
    settings: the orchestrator never trusts an agent to police itself.
    """

    min_green_s: float = 15.0
    # Emergency preemption may shorten a conflicting green to the program's own
    # minDur, but never below this documented floor.
    min_emergency_green_s: float = 4.0
    max_green_s: float = 60.0
    min_yellow_s: float = 3.0
    min_all_red_s: float = 2.0
    max_cycle_s: float = 120.0
    # A single write may never exceed this many seconds of green extension.
    max_extension_s: float = 30.0
    # Reject a write that would leave less than this much green on a stage.
    min_remaining_green_s: float = 3.0
    # Actions are only accepted for traffic lights that exist in the network.
    require_known_tls: bool = True

    def __post_init__(self) -> None:
        if self.min_green_s < 5:
            raise ValueError("min_green_s must be at least 5 seconds")
        if self.max_green_s < self.min_green_s:
            raise ValueError("max_green_s must be >= min_green_s")
        if self.min_yellow_s < 3:
            raise ValueError("min_yellow_s must be at least 3 seconds")
        if self.min_all_red_s < 1:
            raise ValueError("min_all_red_s must be at least 1 second")
        if self.max_extension_s < 1:
            raise ValueError("max_extension_s must be at least 1 second")


@dataclass(frozen=True)
class DecisionSettings:
    """Thresholds used by the deterministic decision engine."""

    # TrafficMind only gets to act on HIGH congestion, or when its own
    # immediate action is an explicit green extension.
    traffic_action_levels: Tuple[str, ...] = ("HIGH",)
    # Do not re-issue the same signal action for a TLS within this window.
    signal_action_cooldown_s: float = 10.0
    # Minimum time between two traffic decisions for the same junction.
    traffic_cooldown_s: float = 15.0
    # Parking advice is advisory only; it never touches signals.
    parking_top_n: int = 3
    # Ignore parking demand alerts below this occupancy percentage.
    parking_alert_occupancy_pct: float = 80.0
    # Maximum number of decisions retained in memory.
    decision_history_size: int = 500


@dataclass(frozen=True)
class OrchestratorConfig:
    """Top-level orchestrator settings."""

    # --- simulation ---
    sumocfg: Path = DEFAULT_SUMOCFG
    zones_file: Path = DEFAULT_ZONES_FILE
    label: str = "orchestrator"
    seed: int = 42
    #: Program ID of the authored signal plans in ``signals.add.xml``. The
    #: coordinator activates it once at startup so every agent reads the same,
    #: safety-checkable program. Empty string keeps SUMO's default program.
    signal_program_id: str = "orchestrator"
    step_length: float = 1.0
    end_time: int = 1800
    gui: bool = False
    gui_delay_ms: int = 100
    connect_retries: int = 3
    # SUMO demand multiplier. 1.0 is the scenario as written; higher values
    # create congestion, which the comparison harness needs.
    demand_scale: float = 1.0

    # --- orchestration ---
    # How often (in simulation seconds) the full agent pipeline is sampled.
    decision_interval_s: float = 5.0
    # How often parking occupancy is read from SUMO.
    parking_interval_s: float = 10.0
    # Event dedupe window.
    event_dedupe_window_s: float = 30.0
    # Bounded event queue.
    max_pending_events: int = 1000
    # Emergency vehicles to track (discovered from SUMO when empty).
    emergency_vehicle_prefixes: Tuple[str, ...] = ("amb_", "ambulance")
    # Destination used for parking recommendations.
    parking_destination: str = "hospital"

    # --- feature switches (useful for the comparison harness) ---
    enable_traffic_mind: bool = True
    enable_green_corridor: bool = True
    enable_park_sense: bool = True
    # When False the coordinator never writes to SUMO (fixed-time baseline).
    enable_signal_control: bool = True

    safety: SafetyLimits = field(default_factory=SafetyLimits)
    decisions: DecisionSettings = field(default_factory=DecisionSettings)

    def __post_init__(self) -> None:
        if self.decision_interval_s <= 0:
            raise ValueError("decision_interval_s must be positive")
        if self.parking_interval_s <= 0:
            raise ValueError("parking_interval_s must be positive")
        if self.event_dedupe_window_s < 0:
            raise ValueError("event_dedupe_window_s must be >= 0")
        if self.max_pending_events < 1:
            raise ValueError("max_pending_events must be >= 1")
        if self.end_time <= 0:
            raise ValueError("end_time must be positive")

    def resolved_sumocfg(self) -> Path:
        return Path(self.sumocfg)

    def resolved_zones_file(self) -> Path:
        return Path(self.zones_file)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure console logging (called by scripts, never on import)."""
    logging.basicConfig(level=level, format=LOG_FORMAT)


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable (``1/true/yes/on``)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
