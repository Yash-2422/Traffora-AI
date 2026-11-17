"""Configuration for the TrafficMind agent.

All paths and tunable values live here so nothing is hardcoded in the logic
modules. Thresholds for congestion, prediction and signals are added in later
steps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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

    def resolved_sumocfg(self) -> Path:
        if self.sumocfg is not None:
            return Path(self.sumocfg)
        return SCENARIO_DIR / f"{self.scenario}.sumocfg"


def configure_logging(level: int = logging.INFO) -> None:
    """Set up console logging (used by scripts and tests, not on import)."""
    logging.basicConfig(level=level, format=LOG_FORMAT)