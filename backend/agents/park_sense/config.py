"""ParkSense configuration: file paths, thresholds, weights and logging."""

import logging
from pathlib import Path

# ---------- Paths ----------
# config.py lives in backend/agents/park_sense, so the project root is 3 levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

ZONES_FILE = PROJECT_ROOT / "simulation" / "park_sense" / "parking_data" / "zones.json"
SUMO_CONFIG_FILE = PROJECT_ROOT / "simulation" / "park_sense" / "scenarios" / "parksense.sumocfg"
HISTORY_FILE = PROJECT_ROOT / "datasets" / "park_sense" / "historical_occupancy.csv"
MODEL_FILE = PROJECT_ROOT / "datasets" / "park_sense" / "demand_model.pkl"

DEFAULT_DESTINATION = "hospital"

# ---------- Demand classification (occupancy percent) ----------
DEMAND_MEDIUM_THRESHOLD = 50.0   # below this -> Low
DEMAND_HIGH_THRESHOLD = 80.0     # at or above this -> High
# between the two -> Medium

# ---------- Recommendation engine ----------
# Weights must add up to 1.0
WEIGHT_AVAILABILITY = 0.35
WEIGHT_DISTANCE = 0.40
WEIGHT_DEMAND = 0.25

MIN_AVAILABLE_SPACES = 1          # zones with fewer free spaces are excluded
AVERAGE_SPEED_MPS = 8.0           # average urban driving speed used for travel time

# ---------- Prediction ----------
PREDICTION_HORIZON_MINUTES = 30
MIN_HISTORY_ROWS = 200            # below this, use the rule-based baseline only

# ---------- Logging ----------
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def setup_logging(level: int = LOG_LEVEL) -> None:
    """Configure logging once for the whole ParkSense agent."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)
    else:
        root.setLevel(level)