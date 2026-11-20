"""ParkSense demand predictor.

Predicts parking occupancy 30 minutes ahead and classifies demand as
Low, Medium or High.

Methods:
  1. rule_based       - current occupancy + the usual hourly pattern of the zone
  2. machine_learning - Random Forest trained on historical_occupancy.csv

Run from the project root:
    python -m backend.agents.park_sense.demand_predictor --train
    python -m backend.agents.park_sense.demand_predictor --demo
"""

import argparse
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error

from . import config
from .schemas import DemandPrediction, InvalidParkingDataError, ZoneStatus

logger = logging.getLogger(__name__)

STEP_MINUTES = 15  # spacing of rows in the history file
REQUIRED_COLUMNS = {"timestamp", "zone_id", "capacity", "occupied", "occupancy_pct"}
FEATURE_COLUMNS = ["zone_code", "hour", "minute", "day_of_week", "is_weekend", "current_pct", "prev_pct"]


# ---------- basic helpers ----------
def classify_demand(occupancy_pct: float) -> str:
    """Convert an occupancy percentage into 'Low', 'Medium' or 'High'.

    Input:  occupancy_pct (0 to 100)
    Output: "Low" (< 50), "Medium" (50 to < 80) or "High" (>= 80)
    """
    if isinstance(occupancy_pct, bool) or not isinstance(occupancy_pct, (int, float, np.floating, np.integer)):
        raise InvalidParkingDataError(f"occupancy_pct must be a number, got {occupancy_pct!r}")
    if np.isnan(occupancy_pct) or occupancy_pct < 0 or occupancy_pct > 100:
        raise InvalidParkingDataError(f"occupancy_pct must be between 0 and 100, got {occupancy_pct!r}")
    if occupancy_pct >= config.DEMAND_HIGH_THRESHOLD:
        return "High"
    if occupancy_pct >= config.DEMAND_MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


def load_history(path=None) -> pd.DataFrame:
    """Load and validate the historical occupancy CSV.

    Output: DataFrame with columns timestamp, zone_id, capacity, occupied, occupancy_pct, ...
    Raises: InvalidParkingDataError if the file is missing or has wrong columns
    """
    path = path or config.HISTORY_FILE
    try:
        df = pd.read_csv(path)
    except FileNotFoundError as exc:
        raise InvalidParkingDataError(f"History file not found: {path}") from exc
    except Exception as exc:
        raise InvalidParkingDataError(f"Could not read history file: {exc}") from exc

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise InvalidParkingDataError(f"History file is missing columns: {sorted(missing)}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["occupancy_pct"] = pd.to_numeric(df["occupancy_pct"], errors="coerce")
    before = len(df)
    df = df.dropna(subset=["timestamp", "occupancy_pct"])
    df = df[(df["occupancy_pct"] >= 0) & (df["occupancy_pct"] <= 100)]
    if len(df) < before:
        logger.warning("Dropped %d invalid history rows", before - len(df))

    logger.info("Loaded %d history rows", len(df))
    return df.reset_index(drop=True)


def _build_profile(history: pd.DataFrame) -> Dict[tuple, float]:
    """Average occupancy per (zone, weekend?, hour). This is the 'usual pattern'."""
    data = history.copy()
    data["hour"] = data["timestamp"].dt.hour
    data["is_weekend"] = (data["timestamp"].dt.day_of_week >= 5).astype(int)
    grouped = data.groupby(["zone_id", "is_weekend", "hour"])["occupancy_pct"].mean()
    return {(z, int(w), int(h)): float(v) for (z, w, h), v in grouped.items()}


def _rule_based_value(profile: dict, zone_id: str, now, horizon_minutes: int, current_pct: float) -> float:
    """Rule: future = current + (usual level later - usual level now)."""
    future = now + timedelta(minutes=horizon_minutes)
    key_now = (zone_id, int(now.weekday() >= 5), now.hour)
    key_future = (zone_id, int(future.weekday() >= 5), future.hour)
    if key_now in profile and key_future in profile:
        value = current_pct + (profile[key_future] - profile[key_now])
    else:
        value = current_pct  # no pattern known: assume no change
    return float(min(100.0, max(0.0, value)))


def _build_training_table(history: pd.DataFrame, steps: int, zone_codes: Dict[str, int]) -> pd.DataFrame:
    """Turn history rows into (features, target) rows. Target = occupancy `steps` rows later."""
    df = history.sort_values(["zone_id", "timestamp"]).copy()
    grouped = df.groupby("zone_id")["occupancy_pct"]
    df["current_pct"] = df["occupancy_pct"]
    df["prev_pct"] = grouped.shift(1)
    df["target_pct"] = grouped.shift(-steps)
    df["hour"] = df["timestamp"].dt.hour
    df["minute"] = df["timestamp"].dt.minute
    df["day_of_week"] = df["timestamp"].dt.day_of_week
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
    df["zone_code"] = df["zone_id"].map(zone_codes)
    return df.dropna(subset=["prev_pct", "target_pct", "zone_code"])


# ---------- the predictor ----------
class DemandPredictor:
    """Predicts future occupancy and demand level for parking zones."""

    def __init__(self, history: Optional[pd.DataFrame] = None, horizon_minutes: Optional[int] = None):
        self.horizon = horizon_minutes or config.PREDICTION_HORIZON_MINUTES
        self.history = history
        self.profile: Dict[tuple, float] = _build_profile(history) if history is not None and len(history) else {}
        self.model: Optional[RandomForestRegressor] = None
        self.zone_codes: Dict[str, int] = {}
        self.metrics: dict = {}

    @classmethod
    def from_files(cls) -> "DemandPredictor":
        """Create a predictor from the default history file and saved model (both optional)."""
        try:
            history = load_history()
        except InvalidParkingDataError as exc:
            logger.warning("%s. Rule-based prediction will use current occupancy only.", exc)
            history = None
        predictor = cls(history)
        predictor.load_model()
        return predictor

    # ----- training -----
    def train(self) -> dict:
        """Train the Random Forest, compare it with the rule-based baseline and save it.

        Output: dict of metrics (mean absolute error and demand-level accuracy)
        """
        if self.history is None or len(self.history) < config.MIN_HISTORY_ROWS:
            raise InvalidParkingDataError(
                f"Need at least {config.MIN_HISTORY_ROWS} history rows to train the model"
            )

        steps = max(1, self.horizon // STEP_MINUTES)
        zone_codes = {z: i for i, z in enumerate(sorted(self.history["zone_id"].unique()))}
        table = _build_training_table(self.history, steps, zone_codes)

        # Time-based split: first 80% of the time range trains, last 20% tests.
        times = np.sort(table["timestamp"].unique())
        cutoff = times[int(len(times) * 0.8)]
        train_part = table[table["timestamp"] < cutoff]
        test_part = table[table["timestamp"] >= cutoff]
        if train_part.empty or test_part.empty:
            raise InvalidParkingDataError("Not enough history to split into training and test data")

        def new_model() -> RandomForestRegressor:
            return RandomForestRegressor(n_estimators=150, min_samples_leaf=3, random_state=42, n_jobs=-1)

        model = new_model().fit(train_part[FEATURE_COLUMNS], train_part["target_pct"])
        ml_pred = np.clip(model.predict(test_part[FEATURE_COLUMNS]), 0, 100)

        # Baseline uses only the training period, so the comparison is fair.
        train_profile = _build_profile(self.history[self.history["timestamp"] < cutoff])
        rule_pred = np.array([
            _rule_based_value(train_profile, r.zone_id, r.timestamp, self.horizon, r.current_pct)
            for r in test_part.itertuples()
        ])

        actual = test_part["target_pct"].to_numpy()
        persistence = test_part["current_pct"].to_numpy()  # "nothing changes" guess

        def level_accuracy(pred: np.ndarray) -> float:
            hits = [classify_demand(float(p)) == classify_demand(float(a)) for p, a in zip(pred, actual)]
            return round(float(np.mean(hits)) * 100, 1)

        self.metrics = {
            "horizon_minutes": self.horizon,
            "train_rows": int(len(train_part)),
            "test_rows": int(len(test_part)),
            "mae_no_change_pct": round(float(mean_absolute_error(actual, persistence)), 2),
            "mae_rule_based_pct": round(float(mean_absolute_error(actual, rule_pred)), 2),
            "mae_machine_learning_pct": round(float(mean_absolute_error(actual, ml_pred)), 2),
            "level_accuracy_rule_based_pct": level_accuracy(rule_pred),
            "level_accuracy_machine_learning_pct": level_accuracy(ml_pred),
        }

        # Final model uses all data.
        self.model = new_model().fit(table[FEATURE_COLUMNS], table["target_pct"])
        self.zone_codes = zone_codes
        self.save_model()
        logger.info("Model trained: %s", self.metrics)
        return self.metrics

    def save_model(self) -> None:
        config.MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"model": self.model, "zone_codes": self.zone_codes,
             "horizon": self.horizon, "metrics": self.metrics},
            config.MODEL_FILE,
        )
        logger.info("Model saved to %s", config.MODEL_FILE)

    def load_model(self) -> bool:
        """Load the saved model. Returns False if none is available."""
        if not config.MODEL_FILE.exists():
            logger.info("No saved model found, using rule-based prediction")
            return False
        try:
            bundle = joblib.load(config.MODEL_FILE)
            if bundle["horizon"] != self.horizon:
                logger.warning("Saved model horizon differs from the current setting; ignoring it")
                return False
            self.model = bundle["model"]
            self.zone_codes = bundle["zone_codes"]
            self.metrics = bundle.get("metrics", {})
            return True
        except Exception as exc:  # corrupt or incompatible file
            logger.error("Could not load model (%s). Using rule-based prediction.", exc)
            self.model = None
            return False

    # ----- prediction -----
    def predict(
        self,
        statuses: List[ZoneStatus],
        now: Optional[datetime] = None,
        previous_pct: Optional[Dict[str, float]] = None,
        method: str = "auto",
    ) -> List[DemandPrediction]:
        """Predict demand for each zone.

        Input:
          statuses      - list of ZoneStatus (from the occupancy monitor)
          now           - current datetime (defaults to the real time)
          previous_pct  - optional {zone_id: occupancy % about 15 minutes ago}
          method        - "auto" (ML if available), "rule_based" or "machine_learning"
        Output: list of DemandPrediction
        """
        if not statuses:
            raise InvalidParkingDataError("No zone statuses were given to predict_demand")
        if method not in ("auto", "rule_based", "machine_learning"):
            raise InvalidParkingDataError(f"Unknown prediction method: {method!r}")

        now = now or datetime.now()
        previous_pct = previous_pct or {}
        results = []

        for status in statuses:
            current = float(status.occupancy_pct)
            use_ml = (
                method != "rule_based"
                and self.model is not None
                and status.zone_id in self.zone_codes
            )
            predicted, used = None, "rule_based"

            if use_ml:
                try:
                    row = pd.DataFrame([{
                        "zone_code": self.zone_codes[status.zone_id],
                        "hour": now.hour,
                        "minute": now.minute,
                        "day_of_week": now.weekday(),
                        "is_weekend": int(now.weekday() >= 5),
                        "current_pct": current,
                        "prev_pct": float(previous_pct.get(status.zone_id, current)),
                    }])[FEATURE_COLUMNS]
                    predicted = float(np.clip(self.model.predict(row)[0], 0, 100))
                    used = "machine_learning"
                except Exception as exc:
                    logger.error("ML prediction failed for %s (%s). Falling back to rules.", status.zone_id, exc)

            if predicted is None:
                if method == "machine_learning":
                    logger.warning("ML requested but unavailable for %s; using rule-based", status.zone_id)
                predicted = _rule_based_value(self.profile, status.zone_id, now, self.horizon, current)

            predicted = round(predicted, 1)
            results.append(DemandPrediction(
                zone_id=status.zone_id,
                current_occupancy_pct=current,
                predicted_occupancy_pct=predicted,
                demand_level=classify_demand(predicted),
                method=used,
                horizon_minutes=self.horizon,
            ))
        return results


# ---------- demo ----------
def run_demo() -> None:
    from .occupancy_monitor import load_zones

    predictor = DemandPredictor.from_files()
    print(f"ML model available: {predictor.model is not None}")
    print(f"History patterns available: {len(predictor.profile) > 0}")

    zones = load_zones()
    sample_occupied = {"PA_A": 5, "PA_B": 3, "PA_C": 2, "PA_D": 12, "PA_E": 1}
    statuses = [ZoneStatus.build(z, sample_occupied[z.zone_id]) for z in zones]
    now = datetime(2026, 11, 24, 9, 30)  # a Tuesday morning
    print(f"\nSample time: {now:%A %H:%M}, predicting {predictor.horizon} minutes ahead\n")

    rule = {p.zone_id: p for p in predictor.predict(statuses, now, method="rule_based")}
    ml = {p.zone_id: p for p in predictor.predict(statuses, now, method="auto")}

    print(f"{'Zone':<6}{'Now %':>8}{'Rule %':>9}{'Rule level':>12}{'ML %':>8}{'ML level':>10}  Method")
    for s in statuses:
        r, m = rule[s.zone_id], ml[s.zone_id]
        print(f"{s.zone_id:<6}{s.occupancy_pct:>8}{r.predicted_occupancy_pct:>9}{r.demand_level:>12}"
              f"{m.predicted_occupancy_pct:>8}{m.demand_level:>10}  {m.method}")

    print("\nJSON output for PA_A:")
    print(json.dumps(ml["PA_A"].to_dict(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="ParkSense demand predictor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--train", action="store_true", help="train and save the ML model")
    group.add_argument("--demo", action="store_true", help="show sample predictions")
    args = parser.parse_args()
    config.setup_logging(logging.WARNING)

    if args.train:
        predictor = DemandPredictor(load_history())
        print(json.dumps(predictor.train(), indent=2))
        print(f"\nModel saved to {config.MODEL_FILE}")
    else:
        run_demo()


if __name__ == "__main__":
    main()