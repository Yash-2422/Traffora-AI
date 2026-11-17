"""Central configuration for GreenCorridor. No magic numbers elsewhere in the module."""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------
# .../backend/agents/green_corridor/config.py -> repo root is 3 levels up
REPO_ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = REPO_ROOT / "simulation" / "green_corridor"
NETWORK_DIR = SIM_DIR / "sample_network"
SCENARIO_DIR = SIM_DIR / "scenarios"
RESULTS_DIR = SIM_DIR / "results"

# --- SUMO / TraCI ----------------------------------------------------------
SUMO_BINARY = "sumo"          # headless
SUMO_GUI_BINARY = "sumo-gui"  # with GUI
SIM_STEP_LENGTH_S = float(os.environ.get("GC_STEP_LENGTH", "1.0"))
DEFAULT_SEED = int(os.environ.get("GC_SEED", "42"))
TRACI_START_TIMEOUT_S = 20    # seconds to wait for SUMO to accept the TraCI connection

# --- Emergency vehicle identification --------------------------------------
EMERGENCY_VTYPE_IDS = ("ambulance",)       # SUMO vType ids treated as emergency
EMERGENCY_VCLASS = "emergency"             # SUMO vClass treated as emergency
EMERGENCY_ID_PREFIXES = ("amb_", "ambulance")  # fallback match on vehicle id