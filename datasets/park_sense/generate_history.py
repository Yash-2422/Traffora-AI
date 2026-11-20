"""Generate simulated historical parking occupancy for ParkSense.

Run from the project root:
    python datasets/park_sense/generate_history.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ZONES_FILE = ROOT / "simulation" / "park_sense" / "parking_data" / "zones.json"
OUT_FILE = Path(__file__).resolve().parent / "historical_occupancy.csv"

START = pd.Timestamp("2026-11-02 00:00")  # a Monday
DAYS = 14
STEP_MINUTES = 15

rng = np.random.default_rng(42)  # fixed seed -> same data every run

# base level, weekday peaks (center_hour, width_hours, height), weekend multiplier
PROFILES = {
    "PA_A": {"base": 0.15, "peaks": [(10, 2.5, 0.70), (15, 2.5, 0.55)], "weekend": 0.6},
    "PA_B": {"base": 0.10, "peaks": [(13, 2.0, 0.35), (19, 2.5, 0.60)], "weekend": 1.4},
    "PA_C": {"base": 0.05, "peaks": [(9, 2.0, 0.75), (12, 2.0, 0.30)], "weekend": 0.8},
    "PA_D": {"base": 0.15, "peaks": [(8, 1.5, 0.55), (18, 2.0, 0.55)], "weekend": 0.7},
    "PA_E": {"base": 0.05, "peaks": [(20, 2.0, 0.85)], "weekend": 1.3},
}


def bump(hour: float, center: float, width: float, height: float) -> float:
    """A smooth peak around `center` hour."""
    return height * np.exp(-((hour - center) ** 2) / (2 * width ** 2))


def main() -> None:
    with open(ZONES_FILE, "r", encoding="utf-8") as f:
        zones = json.load(f)["zones"]

    timestamps = pd.date_range(START, periods=DAYS * 24 * 60 // STEP_MINUTES, freq=f"{STEP_MINUTES}min")
    rows = []

    for ts in timestamps:
        hour = ts.hour + ts.minute / 60
        is_weekend = ts.dayofweek >= 5
        for zone in zones:
            profile = PROFILES[zone["zone_id"]]
            ratio = profile["base"] + sum(bump(hour, *p) for p in profile["peaks"])
            if is_weekend:
                ratio *= profile["weekend"]
            ratio += rng.normal(0, 0.05)          # random noise
            ratio = float(np.clip(ratio, 0.0, 1.0))
            capacity = zone["capacity"]
            occupied = int(round(ratio * capacity))
            rows.append({
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "zone_id": zone["zone_id"],
                "capacity": capacity,
                "occupied": occupied,
                "occupancy_pct": round(occupied / capacity * 100, 1),
                "hour": ts.hour,
                "day_of_week": ts.dayofweek,
                "is_weekend": int(is_weekend),
            })

    df = pd.DataFrame(rows)
    df.to_csv(OUT_FILE, index=False)
    print(f"Saved {len(df)} rows to {OUT_FILE}")


if __name__ == "__main__":
    main()