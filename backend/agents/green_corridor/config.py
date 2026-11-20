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

# --- Route planning (Step 5) ----------------------------------------------------
ALLOW_U_TURNS = False             # exclude U-turn connections from emergency routes
RECALC_MIN_GAIN_S = 20.0          # re-route only if the alternative saves at least this many seconds...
RECALC_MIN_GAIN_FRACTION = 0.15   # ...and at least this fraction of the current remaining ETA
MAX_EDGE_DELAY_FACTOR = 5.0       # cap per-edge live travel time at this multiple of free-flow
HARMONIC_MIN_SPEED_MPS = 0.5      # floor for a vehicle's speed when averaging (a standing car counts as 0.5 m/s)

# --- Intersection monitoring (Step 6) ---------------------------------------------
INTERSECTION_LOOKAHEAD_M = 600.0     # default look-ahead for identify_upcoming_intersections()
WAIT_ATTRIBUTION_ZONE_M = 100.0      # stopped time is attributed to the next signal if its stop line is this close 