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

# --- Scenario defaults (Step 3) ---------------------------------------------
SCENARIO_META_FILENAME = "scenario.json"
AMBULANCE_VTYPE_ID = EMERGENCY_VTYPE_IDS[0]

# --- Detection / tracking (Step 4) --------------------------------------------
STOPPED_SPEED_MPS = 0.1          # below this the vehicle is considered stopped
ACCEL_THRESHOLD_MPS2 = 0.3       # |acceleration| above this -> accelerating/decelerating
TRACK_HISTORY_LEN = 600          # max trail points kept per vehicle (for visualisation)
SUMO_INVALID_DOUBLE = -1073741824.0  # TraCI's "no value" marker